import os
import re
import uuid
import tempfile
import hashlib
import hmac
import time
import smtplib
import secrets
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from email.message import EmailMessage
from email.utils import formataddr

import requests

from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
    status,
    Request,
    BackgroundTasks,
    File,
    UploadFile,
    Form,
    Cookie,
)

from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
)

from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field

from models import (
    SessionLocal,
    EmployerCache,
    MonthlyLedger,
    User,
    AdminSettings,
    Payment,
    EmployeeDetail,
    VisitorSession,
    VisitorEvent,
    Form16Generation,
    EmailDelivery,
)

from core.parser import load_and_parse_pdf


try:
    from pdf_generator import generate_form16_pdf
except ImportError:
    generate_form16_pdf = None


app = FastAPI(
    title="Form 16 Generator API",
    version="1.0",
)

templates = Jinja2Templates(directory="templates")


# =========================================================
# DATABASE
# =========================================================

def get_db():
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()


# =========================================================
# HELPERS
# =========================================================

def validate_financial_year(financial_year: str) -> str:
    """
    Expected format: YYYY-YY
    Example: 2026-27
    """

    if not financial_year:
        raise ValueError("Financial year is required.")

    financial_year = financial_year.strip()

    match = re.fullmatch(r"(\d{4})-(\d{2})", financial_year)

    if not match:
        raise ValueError(
            "Invalid financial year. Expected format YYYY-YY."
        )

    start_year = int(match.group(1))
    end_suffix = int(match.group(2))

    expected_suffix = (start_year + 1) % 100

    if end_suffix != expected_suffix:
        raise ValueError(
            "Financial year must contain consecutive years."
        )

    return financial_year


def get_active_financial_year(db: Session) -> str:
    settings = db.query(AdminSettings).first()

    if not settings or not settings.financial_year:
        raise HTTPException(
            status_code=400,
            detail="Admin has not selected the financial year yet.",
        )

    try:
        return validate_financial_year(settings.financial_year)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )


def month_year_string(year: int, month: int) -> str:
    if month < 1 or month > 12:
        raise ValueError("Month must be between 1 and 12.")

    return f"{int(year):04d}-{int(month):02d}"


def month_belongs_to_financial_year(
    month: int,
    year: int,
    financial_year: str,
) -> bool:

    financial_year = validate_financial_year(financial_year)

    start_year = int(financial_year[:4])
    end_year = start_year + 1

    # Payroll/Form-16 cycle:
    # March -> December = start year
    # January -> February = end year

    if 3 <= month <= 12:
        return year == start_year

    if month in (1, 2):
        return year == end_year

    return False


def get_slip_months(slip: dict) -> list:
    """
    Normalises parser period information into:
    [(month, year), ...]
    """

    period = slip.get("period") or {}
    months = period.get("months") or []

    result = []

    for item in months:
        if not isinstance(item, (list, tuple)):
            continue

        if len(item) < 2:
            continue

        try:
            month = int(item[0])
            year = int(item[1])
        except (TypeError, ValueError):
            continue

        if 1 <= month <= 12:
            result.append((month, year))

    return result



# =========================================================
# VISITOR / EMAIL / FORM-16 HELPERS
# =========================================================

PUBLIC_PAGE_STEPS = {
    "/": ("upload", "upload", 10),
    "/review": ("review", "review", 35),
    "/ddo-details": ("ddo_details", "ddo_details", 60),
    "/payment": ("payment", "payment", 80),
}

REMINDER_DELAY_HOURS = int(os.getenv("REMINDER_DELAY_HOURS", "6"))


def assessment_year_from_financial_year(financial_year: str) -> str:
    financial_year = validate_financial_year(financial_year)
    start_year = int(financial_year[:4])
    return f"{start_year + 1}-{(start_year + 2) % 100:02d}"


def normalise_email(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip().lower()
    if not value:
        return None

    if not re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
        r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+",
        value,
    ):
        raise ValueError("Valid email address is required.")

    return value


def normalise_mobile(value: Optional[str]) -> Optional[str]:
    value = re.sub(r"\D", "", value or "")

    if not value:
        return None

    if len(value) == 12 and value.startswith("91"):
        value = value[2:]

    if not re.fullmatch(r"[6-9]\d{9}", value):
        raise ValueError("Valid 10-digit Indian mobile number is required.")

    return value


def safe_snapshot(data: Optional[dict]) -> dict:
    data = data or {}
    allowed = {
        "name",
        "pan",
        "office_school_name",
        "tan_id",
    }

    result = {}
    for key in allowed:
        value = data.get(key)
        if value is not None:
            result[key] = str(value)[:500]

    return result


def add_visitor_event(
    db: Session,
    visitor_session: VisitorSession,
    event_type: str,
    page_name: Optional[str] = None,
    step_name: Optional[str] = None,
    event_data: Optional[dict] = None,
):
    event = VisitorEvent(
        id=str(uuid.uuid4()),
        session_id=visitor_session.id,
        visitor_id=visitor_session.visitor_id,
        user_id=visitor_session.user_id,
        event_type=event_type,
        page_name=page_name,
        step_name=step_name,
        event_data_json=event_data or {},
    )
    db.add(event)


def get_or_create_visitor_session(
    db: Session,
    visitor_id: Optional[str],
    journey_id: Optional[str],
) -> tuple[VisitorSession, str, str]:
    visitor_id = (visitor_id or "").strip() or str(uuid.uuid4())
    journey_id = (journey_id or "").strip()

    session = None
    if journey_id:
        session = (
            db.query(VisitorSession)
            .filter(VisitorSession.id == journey_id)
            .first()
        )

    if not session:
        session = VisitorSession(
            id=str(uuid.uuid4()),
            visitor_id=visitor_id,
            current_page="upload",
            current_step="upload",
            progress_percent=10,
            application_status="in_progress",
            payment_status="not_started",
            resume_token=secrets.token_urlsafe(32),
            last_seen_at=datetime.utcnow(),
        )
        db.add(session)
        db.flush()
        add_visitor_event(
            db,
            session,
            "form_started",
            page_name="upload",
            step_name="upload",
        )

    return session, visitor_id, session.id


def find_active_visitor_session(
    db: Session,
    journey_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[VisitorSession]:
    if journey_id:
        session = (
            db.query(VisitorSession)
            .filter(VisitorSession.id == journey_id)
            .first()
        )
        if session:
            return session

    if user_id:
        return (
            db.query(VisitorSession)
            .filter(VisitorSession.user_id == user_id)
            .order_by(VisitorSession.last_seen_at.desc())
            .first()
        )

    return None


def sync_visitor_payment_state(
    db: Session,
    user_id: str,
    payment_status: str,
    application_status: Optional[str] = None,
):
    sessions = (
        db.query(VisitorSession)
        .filter(VisitorSession.user_id == user_id)
        .all()
    )

    now = datetime.utcnow()

    for session in sessions:
        session.payment_status = payment_status
        session.last_seen_at = now

        if application_status:
            session.application_status = application_status

        if payment_status == "approved":
            session.reminder_status = "cancelled"
            session.reminder_due_at = None
            session.completed_at = now
            session.current_page = "completed"
            session.current_step = "completed"
            session.last_completed_step = "payment"
            session.progress_percent = 100

        add_visitor_event(
            db,
            session,
            f"payment_{payment_status}",
            page_name=session.current_page,
            step_name=session.current_step,
        )


def smtp_is_configured() -> bool:
    return bool(
        os.getenv("SMTP_USERNAME")
        and os.getenv("SMTP_PASSWORD")
    )


def send_email_message(
    recipient: str,
    subject: str,
    body: str,
    attachment_path: Optional[str] = None,
    attachment_name: Optional[str] = None,
) -> Optional[str]:
    if not smtp_is_configured():
        raise RuntimeError(
            "Email is not configured. Set SMTP_USERNAME and SMTP_PASSWORD."
        )

    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    from_name = os.getenv("SMTP_FROM_NAME", "Form 16 Support").strip()

    message = EmailMessage()
    message["From"] = formataddr((from_name, username))
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    if attachment_path:
        with open(attachment_path, "rb") as file_obj:
            pdf_bytes = file_obj.read()

        message.add_attachment(
            pdf_bytes,
            maintype="application",
            subtype="pdf",
            filename=attachment_name or "Form16.pdf",
        )

    with smtplib.SMTP(host, port, timeout=20) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(username, password)
        response = server.send_message(message)

    return "smtp-accepted" if not response else str(response)


def queue_email_delivery(
    db: Session,
    recipient_email: str,
    email_type: str,
    subject: str,
    user_id: Optional[str] = None,
    visitor_session_id: Optional[str] = None,
    payment_id: Optional[str] = None,
    generation_id: Optional[str] = None,
    scheduled_for: Optional[datetime] = None,
) -> EmailDelivery:
    delivery = EmailDelivery(
        id=str(uuid.uuid4()),
        user_id=user_id,
        visitor_session_id=visitor_session_id,
        payment_id=payment_id,
        generation_id=generation_id,
        recipient_email=recipient_email,
        email_type=email_type,
        subject=subject,
        status="queued",
        scheduled_for=scheduled_for,
        attempt_count=0,
    )
    db.add(delivery)
    db.flush()
    return delivery


def send_delivery_now(
    db: Session,
    delivery: EmailDelivery,
    body: str,
    attachment_path: Optional[str] = None,
    attachment_name: Optional[str] = None,
) -> bool:
    delivery.status = "sending"
    delivery.attempt_count = int(delivery.attempt_count or 0) + 1
    delivery.updated_at = datetime.utcnow()
    db.commit()

    try:
        provider_message_id = send_email_message(
            recipient=delivery.recipient_email,
            subject=delivery.subject or "Form 16",
            body=body,
            attachment_path=attachment_path,
            attachment_name=attachment_name,
        )

        delivery.status = "sent"
        delivery.provider_message_id = provider_message_id
        delivery.sent_at = datetime.utcnow()
        delivery.error_message = None
        delivery.updated_at = datetime.utcnow()
        db.commit()
        return True

    except Exception as exc:
        delivery.status = "failed"
        delivery.error_message = str(exc)[:1000]
        delivery.updated_at = datetime.utcnow()
        db.commit()
        print(f"Email delivery failed: {exc}")
        return False


def build_form16_payload(
    db: Session,
    user_id: str,
    financial_year: str,
):
    employee = (
        db.query(EmployeeDetail)
        .filter(EmployeeDetail.user_id == user_id)
        .first()
    )

    if not employee:
        raise HTTPException(
            status_code=400,
            detail="Employee details are incomplete.",
        )

    if not employee.tan_id:
        raise HTTPException(
            status_code=400,
            detail="Employer TAN is missing.",
        )

    employer = (
        db.query(EmployerCache)
        .filter(EmployerCache.tan == employee.tan_id)
        .first()
    )

    if not employer:
        raise HTTPException(
            status_code=400,
            detail="Employer details are missing.",
        )

    ledger_rows = (
        db.query(MonthlyLedger)
        .filter(
            MonthlyLedger.user_id == user_id,
            MonthlyLedger.financial_year == financial_year,
        )
        .all()
    )

    if not ledger_rows:
        raise HTTPException(
            status_code=400,
            detail=f"No salary ledger found for FY {financial_year}.",
        )

    payroll_order = {
        3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 8: 6,
        9: 7, 10: 8, 11: 9, 12: 10, 1: 11, 2: 12,
    }

    ledger_rows.sort(
        key=lambda row: (
            payroll_order.get(row.month, 99),
            row.year,
        )
    )

    formatted_ledger = []
    for row in ledger_rows:
        formatted_ledger.append(
            {
                "month": row.month,
                "year": row.year,
                "month_year": row.month_year,
                "financial_year": row.financial_year,
                "basic_pay": row.basic_pay or 0,
                "da": row.da or 0,
                "hra": row.hra or 0,
                "gross_salary": row.gross_salary or 0,
                "line_items": row.line_items_json or {},
                "deductions": row.deductions_json or {},
                "source": row.source,
                "is_auto_generated": row.is_auto_generated,
                "note": row.note,
                "flags": row.flags or [],
            }
        )

    return employee, employer, formatted_ledger


def generate_form16_for_record(
    db: Session,
    generation: Form16Generation,
) -> tuple[str, str]:
    if generate_form16_pdf is None:
        raise HTTPException(
            status_code=500,
            detail="PDF generator is not available.",
        )

    generation.status = "generating"
    db.commit()

    try:
        employee, employer, formatted_ledger = build_form16_payload(
            db,
            generation.user_id,
            generation.financial_year,
        )

        pdf_path = generate_form16_pdf(
            employee=employee,
            employer=employer,
            ledger=formatted_ledger,
            financial_year=generation.financial_year,
        )

        if not pdf_path or not os.path.exists(pdf_path):
            raise RuntimeError("Generated PDF file was not found.")

        safe_name = re.sub(
            r"[^A-Za-z0-9_-]+",
            "_",
            employee.name or "Employee",
        ).strip("_")

        filename = (
            f"Form_16_{safe_name}_FY_"
            f"{generation.financial_year}.pdf"
        )

        generation.status = "generated"
        generation.file_name = filename
        generation.generated_at = datetime.utcnow()
        generation.error_message = None
        db.commit()

        return pdf_path, filename

    except HTTPException:
        generation.status = "failed"
        generation.error_message = "Form 16 data validation failed."
        db.commit()
        raise

    except Exception as exc:
        generation.status = "failed"
        generation.error_message = str(exc)[:1000]
        db.commit()
        raise HTTPException(
            status_code=500,
            detail=f"PDF generation failed: {exc}",
        )


def get_or_create_generation(
    db: Session,
    user_id: str,
    financial_year: str,
    source: str,
    payment_id: Optional[str] = None,
) -> Form16Generation:
    query = db.query(Form16Generation).filter(
        Form16Generation.user_id == user_id,
        Form16Generation.financial_year == financial_year,
        Form16Generation.source == source,
    )

    if payment_id:
        query = query.filter(Form16Generation.payment_id == payment_id)

    existing = (
        query.order_by(Form16Generation.created_at.desc()).first()
    )

    if existing and existing.status in {"generated", "generating", "queued"}:
        return existing

    generation = Form16Generation(
        id=str(uuid.uuid4()),
        user_id=user_id,
        payment_id=payment_id,
        financial_year=financial_year,
        assessment_year=assessment_year_from_financial_year(financial_year),
        source=source,
        status="queued",
    )
    db.add(generation)
    db.flush()
    return generation


def send_form16_email_for_generation(
    generation_id: str,
    email_type: str,
):
    db = SessionLocal()

    try:
        generation = (
            db.query(Form16Generation)
            .filter(Form16Generation.id == generation_id)
            .first()
        )

        if not generation:
            return

        user = (
            db.query(User)
            .filter(User.id == generation.user_id)
            .first()
        )

        employee = (
            db.query(EmployeeDetail)
            .filter(EmployeeDetail.user_id == generation.user_id)
            .first()
        )

        if not user or not user.email or not employee:
            return

        pdf_path, filename = generate_form16_for_record(
            db,
            generation,
        )

        if email_type == "payment_confirmed_form16":
            subject = (
                f"Payment Confirmed – Your Form 16 for "
                f"FY {generation.financial_year}"
            )
            body = (
                f"Dear {employee.name or 'Employee'},\n\n"
                "Your payment has been received and verified successfully.\n\n"
                f"Your Form 16 for Financial Year "
                f"{generation.financial_year} has now been generated and "
                "is attached with this email.\n\n"
                "Please keep this document safely for your income-tax "
                "and official records.\n\n"
                "Payment Status: Verified\n"
                f"Financial Year: {generation.financial_year}\n"
                f"PAN: {employee.pan or 'N/A'}\n\n"
                "Thank you for using our Form 16 service.\n\n"
                "Regards,\nForm 16 Support Team"
            )
        else:
            subject = (
                f"Form 16 Generated by Administrator – "
                f"FY {generation.financial_year}"
            )
            body = (
                f"Dear {employee.name or 'Employee'},\n\n"
                f"Your Form 16 for Financial Year "
                f"{generation.financial_year} has been generated directly "
                "by the authorised administrator and is attached with this "
                "email.\n\n"
                "No payment verification was required for this "
                "administrative generation.\n\n"
                "Generation Type: Administrator Generated\n"
                f"Financial Year: {generation.financial_year}\n"
                f"PAN: {employee.pan or 'N/A'}\n\n"
                "Please retain the attached document for your official "
                "and income-tax records.\n\n"
                "Regards,\nForm 16 Administration Team"
            )

        delivery = queue_email_delivery(
            db,
            recipient_email=user.email,
            email_type=email_type,
            subject=subject,
            user_id=user.id,
            payment_id=generation.payment_id,
            generation_id=generation.id,
        )
        db.commit()

        send_delivery_now(
            db,
            delivery,
            body=body,
            attachment_path=pdf_path,
            attachment_name=filename,
        )

    except Exception as exc:
        print(f"Form 16 email task failed: {exc}")

    finally:
        db.close()


def process_due_reminders(limit: int = 50) -> dict:
    db = SessionLocal()
    now = datetime.utcnow()
    sent = 0
    failed = 0
    skipped = 0

    try:
        sessions = (
            db.query(VisitorSession)
            .filter(
                VisitorSession.reminder_status == "pending",
                VisitorSession.reminder_due_at.isnot(None),
                VisitorSession.reminder_due_at <= now,
                VisitorSession.email.isnot(None),
                VisitorSession.email_contact_consent.is_(True),
                VisitorSession.payment_status != "approved",
            )
            .order_by(VisitorSession.reminder_due_at.asc())
            .limit(limit)
            .all()
        )

        base_url = (
            os.getenv("PUBLIC_BASE_URL")
            or os.getenv("RENDER_EXTERNAL_URL")
            or ""
        ).rstrip("/")

        for session in sessions:
            if not session.resume_token:
                skipped += 1
                continue

            resume_url = (
                f"{base_url}/resume/{session.resume_token}"
                if base_url
                else f"/resume/{session.resume_token}"
            )

            subject = "Complete Your Form 16 Application"
            body = (
                "Hello,\n\n"
                "Your Form 16 application is still incomplete. "
                "You can continue from where you left off using the "
                "resume link below:\n\n"
                f"{resume_url}\n\n"
                f"Last completed step: "
                f"{session.last_completed_step or 'Application started'}\n\n"
                "If you have already completed the process, please ignore "
                "this email.\n\n"
                "Regards,\nForm 16 Support Team"
            )

            delivery = queue_email_delivery(
                db,
                recipient_email=session.email,
                email_type="abandoned_reminder",
                subject=subject,
                user_id=session.user_id,
                visitor_session_id=session.id,
            )
            db.commit()

            ok = send_delivery_now(
                db,
                delivery,
                body=body,
            )

            if ok:
                session.reminder_status = "sent"
                session.reminder_sent_at = datetime.utcnow()
                session.application_status = (
                    "abandoned"
                    if session.application_status == "in_progress"
                    else session.application_status
                )
                session.abandoned_at = (
                    session.abandoned_at or datetime.utcnow()
                )
                sent += 1
            else:
                session.reminder_status = "failed"
                failed += 1

            db.commit()

        return {
            "sent": sent,
            "failed": failed,
            "skipped": skipped,
        }

    finally:
        db.close()


@app.middleware("http")
async def visitor_tracking_middleware(request: Request, call_next):
    path = request.url.path

    if (
        path.startswith("/admin")
        or path.startswith("/api")
        or path.startswith("/static")
        or path.startswith("/docs")
        or path.startswith("/openapi")
        or path == "/favicon.ico"
    ):
        return await call_next(request)

    visitor_id = request.cookies.get("form16_visitor_id")
    journey_id = request.cookies.get("form16_journey_id")

    db = SessionLocal()
    session = None
    new_visitor_id = visitor_id
    new_journey_id = journey_id

    try:
        session, new_visitor_id, new_journey_id = (
            get_or_create_visitor_session(
                db,
                visitor_id,
                journey_id,
            )
        )

        page_info = PUBLIC_PAGE_STEPS.get(path)
        if page_info:
            page_name, step_name, progress = page_info
            session.current_page = page_name
            session.current_step = step_name
            session.progress_percent = max(
                int(session.progress_percent or 0),
                progress,
            )
            session.last_seen_at = datetime.utcnow()

            user_id = request.query_params.get("user_id")
            if user_id:
                session.user_id = user_id

            add_visitor_event(
                db,
                session,
                "page_view",
                page_name=page_name,
                step_name=step_name,
            )

        db.commit()

    except Exception as exc:
        db.rollback()
        print(f"Visitor tracking failed: {exc}")

    finally:
        db.close()

    response = await call_next(request)

    if new_visitor_id and new_visitor_id != visitor_id:
        response.set_cookie(
            "form16_visitor_id",
            new_visitor_id,
            max_age=365 * 24 * 60 * 60,
            httponly=True,
            samesite="lax",
            secure=request_uses_https(request),
        )

    if new_journey_id and new_journey_id != journey_id:
        response.set_cookie(
            "form16_journey_id",
            new_journey_id,
            max_age=30 * 24 * 60 * 60,
            httponly=True,
            samesite="lax",
            secure=request_uses_https(request),
        )

    return response


# =========================================================
# ADMIN SESSION HELPERS
# =========================================================

ADMIN_SESSION_MAX_AGE = 8 * 60 * 60


def _admin_username() -> str:
    return os.getenv("ADMIN_USERNAME", "admin_27")


def _admin_password() -> str:
    return os.getenv("ADMIN_PASSWORD", "@admin_def27")


def _admin_session_secret() -> str:
    # For production, set ADMIN_SESSION_SECRET in Render.
    return os.getenv(
        "ADMIN_SESSION_SECRET",
        _admin_password() + "::form16-session",
    )


def create_admin_session_token() -> str:
    issued_at = str(int(time.time()))
    signature = hmac.new(
        _admin_session_secret().encode("utf-8"),
        issued_at.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{issued_at}.{signature}"


def is_valid_admin_session(token: Optional[str]) -> bool:
    if not token or "." not in token:
        return False

    issued_at_text, supplied_signature = token.split(".", 1)

    try:
        issued_at = int(issued_at_text)
    except (TypeError, ValueError):
        return False

    age = int(time.time()) - issued_at
    if age < 0 or age > ADMIN_SESSION_MAX_AGE:
        return False

    expected_signature = hmac.new(
        _admin_session_secret().encode("utf-8"),
        issued_at_text.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(
        supplied_signature,
        expected_signature,
    )


def require_admin_session(admin_session: Optional[str]) -> None:
    if not is_valid_admin_session(admin_session):
        raise HTTPException(
            status_code=401,
            detail="Admin authentication required.",
        )


def request_uses_https(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    return (
        request.url.scheme == "https"
        or forwarded_proto.lower() == "https"
    )


# =========================================================
# PYDANTIC SCHEMAS
# =========================================================

class EmployerSchema(BaseModel):
    tan: str = Field(..., example="RNCEDNK35")

    officer_name: str
    officer_father_name: str

    employer_name: str
    employer_address: str
    designation: str

    pan: Optional[str] = None


class LedgerSchema(BaseModel):
    user_id: str

    month: int = Field(..., ge=1, le=12)
    year: int

    basic_pay: float = 0
    da: float = 0
    hra: float = 0
    gross_salary: float = 0

    source: str = "extracted"

    line_items: Dict[str, Any] = Field(default_factory=dict)
    deductions: Dict[str, Any] = Field(default_factory=dict)

    note: Optional[str] = None
    flags: list[str] = Field(default_factory=list)


class FinancialYearSchema(BaseModel):
    financial_year: str

class EmployeeDetailSchema(BaseModel):
    user_id: str
    name: str
    pan: str
    office_school_name: Optional[str] = None
    tan_id: Optional[str] = None
    email: Optional[str] = None
    mobile: Optional[str] = None
    visitor_session_id: Optional[str] = None
    email_contact_consent: bool = False


class AdminSettingsSchema(BaseModel):
    fee_amount: float = Field(..., ge=0)
    upi_id: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

# =========================================================
# HTML PAGES
# =========================================================

@app.get("/")
def read_root(request: Request):

    return templates.TemplateResponse(
        request=request,
        name="upload.html",
    )


@app.get("/review")
def review_page(
    request: Request,
    user_id: str = "",
):

    return templates.TemplateResponse(
        request=request,
        name="review.html",
        context={
            "user_id": user_id,
        },
    )


@app.get("/ddo-details")
def ddo_details_page(
    request: Request,
    user_id: str = "",
):

    return templates.TemplateResponse(
        request=request,
        name="ddo_details.html",
        context={
            "user_id": user_id,
        },
    )


@app.get("/payment")
def payment_page(
    request: Request,
    user_id: str = "",
    db: Session = Depends(get_db),
):

    settings = db.query(AdminSettings).first()

    upi_id = (
        settings.upi_id
        if settings and settings.upi_id
        else ""
    )

    fee_amount = (
        settings.fee_amount
        if settings and settings.fee_amount is not None
        else 150.0
    )

    financial_year = (
        settings.financial_year
        if settings
        else None
    )

    return templates.TemplateResponse(
        request=request,
        name="payment.html",
        context={
            "upi_id": upi_id,
            "fee_amount": fee_amount,
            "user_id": user_id,
            "financial_year": financial_year,
        },
    )

# =========================================================
# EMPLOYEE DETAILS
# =========================================================

@app.post("/api/employee")
def save_employee_details(
    data: EmployeeDetailSchema,
    db: Session = Depends(get_db),
):
    user_id = data.user_id.strip()
    name = data.name.strip()
    pan = data.pan.strip().upper()

    try:
        email = normalise_email(data.email)
        mobile = normalise_mobile(data.mobile)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    tan_id = (
        data.tan_id.strip().upper()
        if data.tan_id
        else None
    )

    office_school_name = (
        data.office_school_name.strip()
        if data.office_school_name
        else None
    )

    if not user_id:
        raise HTTPException(
            status_code=400,
            detail="User ID is required.",
        )

    if not name:
        raise HTTPException(
            status_code=400,
            detail="Employee name is required.",
        )

    if not pan:
        raise HTTPException(
            status_code=400,
            detail="Employee PAN is required.",
        )

    user = (
        db.query(User)
        .filter(User.id == user_id)
        .first()
    )

    if not user:
        user = User(id=user_id)
        db.add(user)
        db.flush()

    if email:
        duplicate_email = (
            db.query(User)
            .filter(
                User.email == email,
                User.id != user_id,
            )
            .first()
        )
        if duplicate_email:
            raise HTTPException(
                status_code=400,
                detail="This email is already linked to another user.",
            )
        user.email = email

    if mobile:
        duplicate_mobile = (
            db.query(User)
            .filter(
                User.mobile == mobile,
                User.id != user_id,
            )
            .first()
        )
        if duplicate_mobile:
            raise HTTPException(
                status_code=400,
                detail="This mobile number is already linked to another user.",
            )
        user.mobile = mobile

    if tan_id:
        employer = (
            db.query(EmployerCache)
            .filter(EmployerCache.tan == tan_id)
            .first()
        )

        if not employer:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Employer TAN must be saved "
                    "before linking employee details."
                ),
            )

    employee = (
        db.query(EmployeeDetail)
        .filter(EmployeeDetail.user_id == user_id)
        .first()
    )

    if employee:
        employee.name = name
        employee.pan = pan
        employee.office_school_name = office_school_name

        if tan_id:
            employee.tan_id = tan_id

    else:
        employee = EmployeeDetail(
            id=str(uuid.uuid4()),
            user_id=user_id,
            name=name,
            pan=pan,
            office_school_name=office_school_name,
            tan_id=tan_id,
        )
        db.add(employee)

    visitor_session = find_active_visitor_session(
        db,
        journey_id=data.visitor_session_id,
        user_id=user_id,
    )

    if visitor_session:
        visitor_session.user_id = user_id

        if email:
            visitor_session.email = email

        if mobile:
            visitor_session.mobile = mobile

        visitor_session.current_page = "review"
        visitor_session.current_step = "contact_saved"
        visitor_session.last_completed_step = "review"
        visitor_session.progress_percent = max(
            int(visitor_session.progress_percent or 0),
            45,
        )
        visitor_session.last_seen_at = datetime.utcnow()

        snapshot = dict(visitor_session.form_snapshot_json or {})
        snapshot.update(
            safe_snapshot(
                {
                    "name": name,
                    "pan": pan,
                    "office_school_name": office_school_name,
                    "tan_id": tan_id,
                }
            )
        )
        visitor_session.form_snapshot_json = snapshot

        if email and data.email_contact_consent:
            visitor_session.email_contact_consent = True
            visitor_session.reminder_status = "pending"
            visitor_session.reminder_due_at = (
                datetime.utcnow()
                + timedelta(hours=REMINDER_DELAY_HOURS)
            )

        add_visitor_event(
            db,
            visitor_session,
            "contact_saved",
            page_name="review",
            step_name="contact_saved",
            event_data={
                "has_email": bool(email),
                "has_mobile": bool(mobile),
            },
        )

    try:
        db.commit()
        db.refresh(employee)

    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="Employee details could not be saved.",
        )

    return {
        "message": "Employee details saved successfully.",
        "user_id": employee.user_id,
        "name": employee.name,
        "pan": employee.pan,
        "office_school_name": employee.office_school_name,
        "tan_id": employee.tan_id,
        "email": user.email,
        "mobile": user.mobile,
    }


@app.get("/api/employee/{user_id}")
def get_employee_details(
    user_id: str,
    db: Session = Depends(get_db),
):
    employee = (
        db.query(EmployeeDetail)
        .filter(EmployeeDetail.user_id == user_id)
        .first()
    )

    if not employee:
        raise HTTPException(
            status_code=404,
            detail="Employee details not found.",
        )

    user = (
        db.query(User)
        .filter(User.id == user_id)
        .first()
    )

    return {
        "user_id": employee.user_id,
        "name": employee.name,
        "pan": employee.pan,
        "office_school_name": employee.office_school_name,
        "tan_id": employee.tan_id,
        "email": user.email if user else None,
        "mobile": user.mobile if user else None,
    }


# =========================================================
# VISITOR / APPLICATION PROGRESS API
# =========================================================

class ProgressSchema(BaseModel):
    user_id: Optional[str] = None
    page_name: str
    step_name: str
    last_completed_step: Optional[str] = None
    progress_percent: int = Field(default=0, ge=0, le=100)
    email: Optional[str] = None
    mobile: Optional[str] = None
    email_contact_consent: bool = False
    snapshot: Dict[str, Any] = Field(default_factory=dict)


@app.post("/api/progress")
def save_application_progress(
    data: ProgressSchema,
    request: Request,
    db: Session = Depends(get_db),
):
    journey_id = request.cookies.get("form16_journey_id")
    visitor_id = request.cookies.get("form16_visitor_id")

    session = find_active_visitor_session(
        db,
        journey_id=journey_id,
        user_id=data.user_id,
    )

    if not session:
        session, _, _ = get_or_create_visitor_session(
            db,
            visitor_id,
            journey_id,
        )

    if data.user_id:
        session.user_id = data.user_id

    try:
        email = normalise_email(data.email)
        mobile = normalise_mobile(data.mobile)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    if email:
        session.email = email

    if mobile:
        session.mobile = mobile

    session.current_page = data.page_name.strip()[:100]
    session.current_step = data.step_name.strip()[:100]

    if data.last_completed_step:
        session.last_completed_step = (
            data.last_completed_step.strip()[:100]
        )

    session.progress_percent = max(
        int(session.progress_percent or 0),
        data.progress_percent,
    )
    session.last_seen_at = datetime.utcnow()

    snapshot = dict(session.form_snapshot_json or {})
    snapshot.update(safe_snapshot(data.snapshot))
    session.form_snapshot_json = snapshot

    if email and data.email_contact_consent:
        session.email_contact_consent = True

        if session.payment_status != "approved":
            session.reminder_status = "pending"
            session.reminder_due_at = (
                datetime.utcnow()
                + timedelta(hours=REMINDER_DELAY_HOURS)
            )

    add_visitor_event(
        db,
        session,
        "step_completed"
        if data.last_completed_step
        else "progress_saved",
        page_name=session.current_page,
        step_name=session.current_step,
    )

    db.commit()

    return {
        "message": "Progress saved.",
        "session_id": session.id,
        "resume_token": session.resume_token,
        "progress_percent": session.progress_percent,
    }


@app.get("/resume/{resume_token}")
def resume_application(
    resume_token: str,
    db: Session = Depends(get_db),
):
    session = (
        db.query(VisitorSession)
        .filter(VisitorSession.resume_token == resume_token)
        .first()
    )

    if not session:
        raise HTTPException(
            status_code=404,
            detail="Resume link is invalid or expired.",
        )

    page_routes = {
        "upload": "/",
        "review": "/review",
        "ddo_details": "/ddo-details",
        "payment": "/payment",
        "payment_wait": "/payment",
        "completed": "/",
    }

    target = page_routes.get(
        session.current_page or "",
        "/",
    )

    if session.user_id and target != "/":
        separator = "&" if "?" in target else "?"
        target = (
            f"{target}{separator}user_id={session.user_id}"
        )

    response = RedirectResponse(
        url=target,
        status_code=303,
    )

    response.set_cookie(
        "form16_visitor_id",
        session.visitor_id,
        max_age=365 * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        "form16_journey_id",
        session.id,
        max_age=30 * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
    )

    return response


@app.post("/api/internal/process-reminders")
def process_reminders_endpoint(
    request: Request,
):
    expected = os.getenv("REMINDER_CRON_SECRET", "").strip()
    supplied = request.headers.get("x-reminder-secret", "").strip()

    if not expected or not hmac.compare_digest(expected, supplied):
        raise HTTPException(
            status_code=401,
            detail="Reminder processor authentication failed.",
        )

    return process_due_reminders()


# =========================================================
# EMPLOYER / TAN CACHE
# =========================================================

@app.get(
    "/api/employer/{tan}",
    response_model=EmployerSchema,
)
def get_employer_by_tan(
    tan: str,
    db: Session = Depends(get_db),
):

    tan = tan.strip().upper()

    employer = (
        db.query(EmployerCache)
        .filter(EmployerCache.tan == tan)
        .first()
    )

    if not employer:
        raise HTTPException(
            status_code=404,
            detail=(
                "TAN not found. "
                "Employer details must be entered manually."
            ),
        )

    return employer


@app.post(
    "/api/employer",
    response_model=EmployerSchema,
)
def upsert_employer(
    emp_data: EmployerSchema,
    db: Session = Depends(get_db),
):

    tan = emp_data.tan.strip().upper()

    employer = (
        db.query(EmployerCache)
        .filter(EmployerCache.tan == tan)
        .first()
    )

    if employer:

        employer.officer_name = emp_data.officer_name
        employer.officer_father_name = (
            emp_data.officer_father_name
        )

        employer.employer_name = emp_data.employer_name
        employer.employer_address = (
            emp_data.employer_address
        )

        employer.designation = emp_data.designation
        employer.pan = emp_data.pan

    else:

        employer = EmployerCache(
            tan=tan,
            officer_name=emp_data.officer_name,
            officer_father_name=(
                emp_data.officer_father_name
            ),
            employer_name=emp_data.employer_name,
            employer_address=(
                emp_data.employer_address
            ),
            designation=emp_data.designation,
            pan=emp_data.pan,
        )

        db.add(employer)

    db.commit()
    db.refresh(employer)

    return employer


# =========================================================
# SALARY-SLIP EXTRACTION
# =========================================================

@app.post("/api/extract")
async def extract_salary_slip(
    file: UploadFile = File(...),
):

    filename = file.filename or ""

    if not filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="Only PDF files are allowed.",
        )

    tmp_path = None

    try:

        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=".pdf",
        ) as tmp:

            tmp.write(await file.read())
            tmp_path = tmp.name

        parsed_result = load_and_parse_pdf(tmp_path)

        if not parsed_result.get("success"):

            raise HTTPException(
                status_code=400,
                detail=parsed_result.get(
                    "error",
                    "Failed to parse PDF.",
                ),
            )

        return {
            "message": "Extracted successfully",
            "data": parsed_result.get("slips", []),
        }

    except HTTPException:
        raise

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=f"Parsing Error: {exc}",
        )

    finally:

        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


# =========================================================
# MONTHLY LEDGER
# =========================================================

@app.post(
    "/api/ledger/save",
    status_code=status.HTTP_201_CREATED,
)
def save_monthly_ledger(
    ledger_data: LedgerSchema,
    db: Session = Depends(get_db),
):

    user = (
        db.query(User)
        .filter(User.id == ledger_data.user_id)
        .first()
    )

    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found.",
        )

    financial_year = get_active_financial_year(db)

    if not month_belongs_to_financial_year(
        ledger_data.month,
        ledger_data.year,
        financial_year,
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                f"{ledger_data.month}/{ledger_data.year} "
                f"does not belong to FY {financial_year}."
            ),
        )

    month_year = month_year_string(
        ledger_data.year,
        ledger_data.month,
    )

    existing = (
        db.query(MonthlyLedger)
        .filter(
            MonthlyLedger.user_id
            == ledger_data.user_id,

            MonthlyLedger.month_year
            == month_year,
        )
        .first()
    )

    if existing:

        # Actual salary slip overrides an auto-generated
        # January/February projection.

        if (
            existing.is_auto_generated
            and ledger_data.source != "auto_generated"
        ):

            existing.month = ledger_data.month
            existing.year = ledger_data.year
            existing.financial_year = financial_year

            existing.basic_pay = ledger_data.basic_pay
            existing.da = ledger_data.da
            existing.hra = ledger_data.hra

            existing.gross_salary = (
                ledger_data.gross_salary
            )

            existing.line_items_json = dict(
                ledger_data.line_items
            )

            existing.deductions_json = dict(
                ledger_data.deductions
            )

            existing.source = ledger_data.source
            existing.is_auto_generated = False

            existing.note = ledger_data.note
            existing.flags = list(ledger_data.flags)

            db.commit()
            db.refresh(existing)

            return {
                "message": (
                    "Actual salary slip replaced "
                    "the auto-generated projection."
                ),
                "ledger_id": existing.id,
                "month_year": month_year,
                "financial_year": financial_year,
            }

        raise HTTPException(
            status_code=400,
            detail=(
                f"Ledger entry for {month_year} "
                "already exists."
            ),
        )

    new_ledger = MonthlyLedger(
        id=str(uuid.uuid4()),
        user_id=ledger_data.user_id,

        month=ledger_data.month,
        year=ledger_data.year,

        month_year=month_year,
        financial_year=financial_year,

        basic_pay=ledger_data.basic_pay,
        da=ledger_data.da,
        hra=ledger_data.hra,
        gross_salary=ledger_data.gross_salary,

        source=ledger_data.source,

        is_auto_generated=(
            ledger_data.source == "auto_generated"
        ),

        line_items_json=dict(
            ledger_data.line_items
        ),

        deductions_json=dict(
            ledger_data.deductions
        ),

        note=ledger_data.note,
        flags=list(ledger_data.flags),
    )

    db.add(new_ledger)

    try:
        db.commit()

    except IntegrityError:
        db.rollback()

        raise HTTPException(
            status_code=400,
            detail=(
                f"Ledger entry for {month_year} "
                "already exists."
            ),
        )

    db.refresh(new_ledger)

    return {
        "message": "Salary data stored successfully",
        "ledger_id": new_ledger.id,
        "month_year": month_year,
        "financial_year": financial_year,
    }


# =========================================================
# ADMIN LOGIN
# =========================================================

@app.get(
    "/admin",
    response_class=HTMLResponse,
)
async def admin_page(
    request: Request,
    admin_session: str = Cookie(None),
):
    if is_valid_admin_session(admin_session):
        return RedirectResponse(
            url="/admin/dashboard",
            status_code=303,
        )

    return templates.TemplateResponse(
        request=request,
        name="admin_login.html",
        context={
            "request": request,
            "error": None,
        },
    )


@app.post("/admin/login")
async def admin_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    username_ok = hmac.compare_digest(
        username,
        _admin_username(),
    )
    password_ok = hmac.compare_digest(
        password,
        _admin_password(),
    )

    if username_ok and password_ok:
        response = RedirectResponse(
            url="/admin/dashboard",
            status_code=303,
        )

        response.set_cookie(
            key="admin_session",
            value=create_admin_session_token(),
            max_age=ADMIN_SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=request_uses_https(request),
        )

        return response

    return templates.TemplateResponse(
        request=request,
        name="admin_login.html",
        context={
            "request": request,
            "error": "Galat ID ya Password!",
        },
    )


@app.get("/admin/logout")
async def admin_logout():
    response = RedirectResponse(
        url="/admin",
        status_code=303,
    )
    response.delete_cookie("admin_session")
    return response


# =========================================================
# ADMIN SETTINGS
# =========================================================

@app.post("/api/admin/financial-year")
def set_financial_year(
    data: FinancialYearSchema,
    admin_session: str = Cookie(None),
    db: Session = Depends(get_db),
):
    require_admin_session(admin_session)

    try:
        financial_year = validate_financial_year(
            data.financial_year
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    settings = db.query(AdminSettings).first()

    if not settings:
        settings = AdminSettings(
            financial_year=financial_year,
        )
        db.add(settings)
    else:
        settings.financial_year = financial_year

    db.commit()

    return {
        "message": "Financial year updated.",
        "financial_year": financial_year,
        "assessment_year": (
            f"{int(financial_year[:4]) + 1}-"
            f"{(int(financial_year[:4]) + 2) % 100:02d}"
        ),
    }


@app.post("/api/admin/settings")
def update_admin_settings(
    data: AdminSettingsSchema,
    admin_session: str = Cookie(None),
    db: Session = Depends(get_db),
):
    require_admin_session(admin_session)

    settings = db.query(AdminSettings).first()
    if not settings:
        settings = AdminSettings()
        db.add(settings)

    settings.fee_amount = float(data.fee_amount)
    settings.upi_id = data.upi_id.strip()
    settings.telegram_bot_token = (
        data.telegram_bot_token.strip()
    )
    settings.telegram_chat_id = (
        data.telegram_chat_id.strip()
    )

    db.commit()
    db.refresh(settings)

    return {
        "message": "Admin settings updated.",
        "fee_amount": settings.fee_amount,
        "upi_id": settings.upi_id or "",
        "telegram_bot_token": (
            settings.telegram_bot_token or ""
        ),
        "telegram_chat_id": (
            settings.telegram_chat_id or ""
        ),
    }


# =========================================================
# ADMIN DASHBOARD
# =========================================================

@app.get(
    "/admin/dashboard",
    response_class=HTMLResponse,
)
async def admin_dashboard(
    request: Request,
    admin_session: str = Cookie(None),
    db: Session = Depends(get_db),
):
    if not is_valid_admin_session(admin_session):
        return RedirectResponse(
            url="/admin",
            status_code=303,
        )

    payments = (
        db.query(Payment)
        .order_by(Payment.created_at.desc())
        .all()
    )

    dashboard_data = []

    for payment in payments:
        employee = (
            db.query(EmployeeDetail)
            .filter(EmployeeDetail.user_id == payment.user_id)
            .first()
        )
        user = (
            db.query(User)
            .filter(User.id == payment.user_id)
            .first()
        )

        dashboard_data.append(
            {
                "payment_id": payment.id,
                "id": payment.id,
                "user_id": payment.user_id,
                "name": employee.name if employee else "Unknown Employee",
                "pan": employee.pan if employee else "N/A",
                "email": user.email if user else None,
                "mobile": user.mobile if user else None,
                "utr_number": payment.upi_txn_utr,
                "amount": payment.amount or 0,
                "status": payment.status,
                "created_at": payment.created_at,
                "approved_at": payment.approved_at,
                "is_admin_generated": False,
            }
        )

    settings = db.query(AdminSettings).first()

    financial_year = (
        settings.financial_year
        if settings
        else None
    )

    assessment_year = None
    if financial_year:
        try:
            assessment_year = assessment_year_from_financial_year(
                financial_year
            )
        except ValueError:
            assessment_year = None

    pending_count = sum(
        1 for item in dashboard_data
        if item["status"] == "pending"
    )
    approved_count = sum(
        1 for item in dashboard_data
        if item["status"] == "approved"
    )
    rejected_count = sum(
        1 for item in dashboard_data
        if item["status"] == "rejected"
    )
    total_revenue = sum(
        float(item["amount"] or 0)
        for item in dashboard_data
        if item["status"] == "approved"
    )

    employees = (
        db.query(EmployeeDetail)
        .order_by(EmployeeDetail.name.asc())
        .all()
    )

    visitor_sessions = (
        db.query(VisitorSession)
        .order_by(VisitorSession.last_seen_at.desc())
        .all()
    )

    incomplete_users = []
    for session in visitor_sessions:
        if session.application_status == "completed":
            continue

        if session.payment_status == "approved":
            continue

        employee = None
        if session.user_id:
            employee = (
                db.query(EmployeeDetail)
                .filter(EmployeeDetail.user_id == session.user_id)
                .first()
            )

        incomplete_users.append(
            {
                "session_id": session.id,
                "user_id": session.user_id,
                "name": (
                    employee.name
                    if employee
                    else (session.form_snapshot_json or {}).get("name")
                ),
                "email": session.email,
                "mobile": session.mobile,
                "current_page": session.current_page,
                "current_step": session.current_step,
                "last_completed_step": session.last_completed_step,
                "progress_percent": session.progress_percent or 0,
                "application_status": session.application_status,
                "payment_status": session.payment_status,
                "reminder_status": session.reminder_status,
                "last_seen_at": session.last_seen_at,
            }
        )

    generations = (
        db.query(Form16Generation)
        .order_by(Form16Generation.created_at.desc())
        .all()
    )

    admin_generated = []
    for generation in generations:
        if generation.source != "admin":
            continue

        employee = (
            db.query(EmployeeDetail)
            .filter(EmployeeDetail.user_id == generation.user_id)
            .first()
        )
        user = (
            db.query(User)
            .filter(User.id == generation.user_id)
            .first()
        )

        admin_generated.append(
            {
                "id": generation.id,
                "generation_id": generation.id,
                "user_id": generation.user_id,
                "name": employee.name if employee else "Unknown Employee",
                "pan": employee.pan if employee else "N/A",
                "email": user.email if user else None,
                "financial_year": generation.financial_year,
                "status": generation.status,
                "created_at": generation.created_at,
                "generated_at": generation.generated_at,
                "download_count": generation.download_count or 0,
            }
        )

    unique_visitors = (
        db.query(VisitorSession.visitor_id)
        .distinct()
        .count()
    )

    form_started = db.query(VisitorSession).count()

    payment_page_count = (
        db.query(VisitorSession)
        .filter(
            VisitorSession.progress_percent >= 80
        )
        .count()
    )

    email_sent_count = (
        db.query(EmailDelivery)
        .filter(EmailDelivery.status == "sent")
        .count()
    )

    email_failed_count = (
        db.query(EmailDelivery)
        .filter(EmailDelivery.status == "failed")
        .count()
    )

    return templates.TemplateResponse(
        request=request,
        name="admin_dashboard.html",
        context={
            "request": request,
            "users": dashboard_data,
            "payments": dashboard_data,
            "admin_generated": admin_generated,
            "employees": employees,
            "incomplete_users": incomplete_users,
            "visitor_sessions": visitor_sessions,
            "financial_year": financial_year,
            "assessment_year": assessment_year,
            "fee_amount": (
                settings.fee_amount
                if settings and settings.fee_amount is not None
                else 150.0
            ),
            "upi_id": (
                settings.upi_id
                if settings and settings.upi_id
                else ""
            ),
            "telegram_bot_token": (
                settings.telegram_bot_token
                if settings and settings.telegram_bot_token
                else ""
            ),
            "telegram_chat_id": (
                settings.telegram_chat_id
                if settings and settings.telegram_chat_id
                else ""
            ),
            "stats": {
                "total_users": db.query(User).count(),
                "unique_visitors": unique_visitors,
                "form_started": form_started,
                "incomplete_users": len(incomplete_users),
                "payment_page_reached": payment_page_count,
                "total_revenue": total_revenue,
                "pending_payments": pending_count,
                "approved_payments": approved_count,
                "rejected_payments": rejected_count,
                "admin_generated": len(admin_generated),
                "email_sent": email_sent_count,
                "email_failed": email_failed_count,
            },
        },
    )


# =========================================================
# ADMIN PAYMENT ACTIONS
# =========================================================

@app.post("/admin/payment/approve/{payment_id}")
def approve_payment(
    payment_id: str,
    background_tasks: BackgroundTasks,
    admin_session: str = Cookie(None),
    db: Session = Depends(get_db),
):
    if not is_valid_admin_session(admin_session):
        return RedirectResponse(
            url="/admin",
            status_code=303,
        )

    payment = (
        db.query(Payment)
        .filter(Payment.id == payment_id)
        .first()
    )

    if not payment:
        raise HTTPException(
            status_code=404,
            detail="Payment not found.",
        )

    payment.status = "approved"
    payment.approved_at = datetime.utcnow()

    sync_visitor_payment_state(
        db,
        payment.user_id,
        "approved",
        application_status="completed",
    )

    financial_year = get_active_financial_year(db)

    generation = get_or_create_generation(
        db,
        user_id=payment.user_id,
        financial_year=financial_year,
        source="user_payment",
        payment_id=payment.id,
    )

    db.commit()

    background_tasks.add_task(
        send_form16_email_for_generation,
        generation.id,
        "payment_confirmed_form16",
    )

    return RedirectResponse(
        url="/admin/dashboard",
        status_code=303,
    )


@app.post("/admin/payment/decline/{payment_id}")
def decline_payment(
    payment_id: str,
    admin_session: str = Cookie(None),
    db: Session = Depends(get_db),
):
    if not is_valid_admin_session(admin_session):
        return RedirectResponse(
            url="/admin",
            status_code=303,
        )

    payment = (
        db.query(Payment)
        .filter(Payment.id == payment_id)
        .first()
    )

    if not payment:
        raise HTTPException(
            status_code=404,
            detail="Payment not found.",
        )

    payment.status = "rejected"
    payment.approved_at = None

    sync_visitor_payment_state(
        db,
        payment.user_id,
        "rejected",
        application_status="payment_pending",
    )

    db.commit()

    return RedirectResponse(
        url="/admin/dashboard",
        status_code=303,
    )


@app.get("/api/admin/approve/{payment_id}")
def admin_approve_payment(
    payment_id: str,
    background_tasks: BackgroundTasks,
    admin_session: str = Cookie(None),
    db: Session = Depends(get_db),
):
    require_admin_session(admin_session)

    payment = (
        db.query(Payment)
        .filter(Payment.id == payment_id)
        .first()
    )

    if not payment:
        raise HTTPException(
            status_code=404,
            detail="Payment not found.",
        )

    payment.status = "approved"
    payment.approved_at = datetime.utcnow()

    sync_visitor_payment_state(
        db,
        payment.user_id,
        "approved",
        application_status="completed",
    )

    financial_year = get_active_financial_year(db)

    generation = get_or_create_generation(
        db,
        user_id=payment.user_id,
        financial_year=financial_year,
        source="user_payment",
        payment_id=payment.id,
    )

    db.commit()

    background_tasks.add_task(
        send_form16_email_for_generation,
        generation.id,
        "payment_confirmed_form16",
    )

    return {
        "message": f"Payment {payment_id} successfully approved.",
        "generation_id": generation.id,
    }


# =========================================================
# ADMIN DIRECT FORM-16 GENERATION
# =========================================================

@app.post("/admin/form16/generate/{user_id}")
def admin_generate_form16(
    user_id: str,
    background_tasks: BackgroundTasks,
    admin_session: str = Cookie(None),
    db: Session = Depends(get_db),
):
    if not is_valid_admin_session(admin_session):
        return RedirectResponse(
            url="/admin",
            status_code=303,
        )

    employee = (
        db.query(EmployeeDetail)
        .filter(EmployeeDetail.user_id == user_id)
        .first()
    )

    if not employee:
        raise HTTPException(
            status_code=404,
            detail="Employee details not found.",
        )

    user = (
        db.query(User)
        .filter(User.id == user_id)
        .first()
    )

    if not user or not user.email:
        raise HTTPException(
            status_code=400,
            detail="Employee email is required before admin generation.",
        )

    if not employee.tan_id:
        raise HTTPException(
            status_code=400,
            detail="Employer TAN is missing for this employee.",
        )

    financial_year = get_active_financial_year(db)

    # Validate all required PDF data before creating the audit record.
    build_form16_payload(
        db,
        user_id,
        financial_year,
    )

    generation = Form16Generation(
        id=str(uuid.uuid4()),
        user_id=user_id,
        payment_id=None,
        financial_year=financial_year,
        assessment_year=assessment_year_from_financial_year(
            financial_year
        ),
        source="admin",
        status="queued",
    )

    db.add(generation)
    db.commit()

    background_tasks.add_task(
        send_form16_email_for_generation,
        generation.id,
        "admin_generated_form16",
    )

    return RedirectResponse(
        url=f"/api/download/form16-generation/{generation.id}",
        status_code=303,
    )


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram_notification(
    payment_id: str,
    utr: str,
    user_id: str,
):

    # Background task gets its own DB session.
    db = SessionLocal()

    try:

        admin_settings = (
            db.query(AdminSettings)
            .first()
        )

        if (
            not admin_settings
            or not admin_settings.telegram_bot_token
            or not admin_settings.telegram_chat_id
        ):
            return

        employee = (
            db.query(EmployeeDetail)
            .filter(
                EmployeeDetail.user_id == user_id
            )
            .first()
        )

        emp_name = (
            employee.name
            if employee
            else "Unknown User"
        )

        emp_pan = (
            employee.pan
            if employee
            else "Unknown PAN"
        )

        base_url = (
            os.getenv("PUBLIC_BASE_URL")
            or os.getenv("RENDER_EXTERNAL_URL")
            or ""
        ).rstrip("/")

        approve_link = (
            f"{base_url}/admin/dashboard"
            if base_url
            else "/admin/dashboard"
        )

        message = (
            "New Form 16 Payment Request\n\n"
            f"Name: {emp_name}\n"
            f"PAN: {emp_pan}\n"
            f"UTR No: {utr}\n\n"
            f"Approve: {approve_link}"
        )

        url = (
            "https://api.telegram.org/bot"
            f"{admin_settings.telegram_bot_token}"
            "/sendMessage"
        )

        payload = {
            "chat_id": (
                admin_settings.telegram_chat_id
            ),
            "text": message,
            "disable_web_page_preview": True,
        }

        requests.post(
            url,
            json=payload,
            timeout=5,
        )

    except Exception as exc:

        print(
            f"Telegram Notification Failed: {exc}"
        )

    finally:
        db.close()


# =========================================================
# PAYMENT SUBMISSION
# =========================================================

@app.post("/api/payment/submit")
async def submit_utr(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):

    data = await request.json()

    user_id = str(
        data.get("user_id") or ""
    ).strip()

    utr_number = str(
        data.get("utr_number") or ""
    ).strip()

    amount = data.get("amount")

    if not user_id:
        raise HTTPException(
            status_code=400,
            detail="User ID is required.",
        )

    if not utr_number:
        raise HTTPException(
            status_code=400,
            detail="UTR number is required.",
        )

    try:
        amount = float(amount)

    except (TypeError, ValueError):

        raise HTTPException(
            status_code=400,
            detail="Valid payment amount is required.",
        )

    if amount <= 0:

        raise HTTPException(
            status_code=400,
            detail="Payment amount must be positive.",
        )

    # Payment submission must NOT delete or rebuild salary
    # ledgers. Ledger persistence belongs to ledger workflow.

    base_user = (
        db.query(User)
        .filter(User.id == user_id)
        .first()
    )

    if not base_user:

        base_user = User(id=user_id)
        db.add(base_user)
        db.flush()

    payment_id = str(uuid.uuid4())

    payment = Payment(
        id=payment_id,
        user_id=user_id,
        amount=amount,
        upi_txn_utr=utr_number,
        status="pending",
    )

    db.add(payment)

    try:

        sync_visitor_payment_state(
            db,
            user_id,
            "pending",
            application_status="payment_submitted",
        )
        db.commit()

    except IntegrityError as exc:

        db.rollback()

        error_text = str(exc.orig).upper()

        if "UNIQUE" in error_text:

            raise HTTPException(
                status_code=400,
                detail=(
                    "This UTR number has already been used."
                ),
            )

        raise HTTPException(
            status_code=400,
            detail="Payment could not be saved.",
        )

    background_tasks.add_task(
        send_telegram_notification,
        payment_id,
        utr_number,
        user_id,
    )

    return {
        "message": "UTR submitted successfully",
        "payment_id": payment_id,
    }


@app.get(
    "/api/payment/status/{payment_id}"
)
def check_payment_status(
    payment_id: str,
    db: Session = Depends(get_db),
):

    payment = (
        db.query(Payment)
        .filter(Payment.id == payment_id)
        .first()
    )

    if not payment:

        raise HTTPException(
            status_code=404,
            detail="Payment not found.",
        )

    return {
        "status": payment.status,
    }


# =========================================================
# FORM 16 DOWNLOAD
# =========================================================

def mark_generation_downloaded(
    db: Session,
    generation: Form16Generation,
):
    now = datetime.utcnow()
    generation.download_count = int(generation.download_count or 0) + 1

    if not generation.first_downloaded_at:
        generation.first_downloaded_at = now

    generation.last_downloaded_at = now

    sessions = (
        db.query(VisitorSession)
        .filter(VisitorSession.user_id == generation.user_id)
        .all()
    )

    for session in sessions:
        add_visitor_event(
            db,
            session,
            "form16_downloaded",
            page_name="completed",
            step_name="download",
            event_data={
                "generation_id": generation.id,
                "source": generation.source,
            },
        )

    db.commit()


@app.get("/api/download/form16-generation/{generation_id}")
def download_generation_pdf(
    generation_id: str,
    db: Session = Depends(get_db),
):
    generation = (
        db.query(Form16Generation)
        .filter(Form16Generation.id == generation_id)
        .first()
    )

    if not generation:
        raise HTTPException(
            status_code=404,
            detail="Form 16 generation record not found.",
        )

    pdf_path, filename = generate_form16_for_record(
        db,
        generation,
    )

    mark_generation_downloaded(
        db,
        generation,
    )

    return FileResponse(
        path=pdf_path,
        filename=filename,
        media_type="application/pdf",
    )


@app.get("/api/download/form16/{payment_id}")
def download_pdf(
    payment_id: str,
    db: Session = Depends(get_db),
):
    payment = (
        db.query(Payment)
        .filter(Payment.id == payment_id)
        .first()
    )

    if not payment or payment.status != "approved":
        raise HTTPException(
            status_code=403,
            detail="Payment not approved yet.",
        )

    financial_year = get_active_financial_year(db)

    generation = get_or_create_generation(
        db,
        user_id=payment.user_id,
        financial_year=financial_year,
        source="user_payment",
        payment_id=payment.id,
    )

    db.commit()

    pdf_path, filename = generate_form16_for_record(
        db,
        generation,
    )

    mark_generation_downloaded(
        db,
        generation,
    )

    return FileResponse(
        path=pdf_path,
        filename=filename,
        media_type="application/pdf",
    )

