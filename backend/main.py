import os
import re
import uuid
import tempfile
from datetime import datetime
from typing import Optional, Dict, Any

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

    return {
        "user_id": employee.user_id,
        "name": employee.name,
        "pan": employee.pan,
        "office_school_name": employee.office_school_name,
        "tan_id": employee.tan_id,
    }
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

    if admin_session == "authenticated":

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

    # Existing credentials retained for now.
    # Authentication hardening can be handled separately.

    if (
        username == "admin_27"
        and password == "@admin_def27"
    ):

        response = RedirectResponse(
            url="/admin/dashboard",
            status_code=303,
        )

        response.set_cookie(
            key="admin_session",
            value="authenticated",
            httponly=True,
            samesite="lax",
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
# ADMIN SETTINGS / FINANCIAL YEAR
# =========================================================

@app.post("/api/admin/financial-year")
def set_financial_year(
    data: FinancialYearSchema,
    admin_session: str = Cookie(None),
    db: Session = Depends(get_db),
):

    if admin_session != "authenticated":
        raise HTTPException(
            status_code=401,
            detail="Admin authentication required.",
        )

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


@app.get(
    "/admin/dashboard",
    response_class=HTMLResponse,
)
async def admin_dashboard(
    request: Request,
    admin_session: str = Cookie(None),
    db: Session = Depends(get_db),
):

    if admin_session != "authenticated":

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
            .filter(
                EmployeeDetail.user_id
                == payment.user_id
            )
            .first()
        )

        dashboard_data.append(
            {
                "payment_id": payment.id,
                "id": payment.id,

                "name": (
                    employee.name
                    if employee
                    else "Unknown Employee"
                ),

                "pan": (
                    employee.pan
                    if employee
                    else "N/A"
                ),

                "utr_number": payment.upi_txn_utr,
                "amount": payment.amount,
                "status": payment.status,
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
            start_year = int(financial_year[:4])

            assessment_year = (
                f"{start_year + 1}-"
                f"{(start_year + 2) % 100:02d}"
            )

        except (TypeError, ValueError):
            assessment_year = None

    return templates.TemplateResponse(
        request=request,
        name="admin_dashboard.html",
        context={
            "request": request,
            "users": dashboard_data,
            "payments": dashboard_data,
            "financial_year": financial_year,
            "assessment_year": assessment_year,
        },
    )


# =========================================================
# ADMIN PAYMENT APPROVAL
# =========================================================

@app.post(
    "/admin/payment/approve/{payment_id}"
)
def approve_payment(
    payment_id: str,
    admin_session: str = Cookie(None),
    db: Session = Depends(get_db),
):

    if admin_session != "authenticated":

        return RedirectResponse(
            url="/admin",
            status_code=303,
        )

    payment = (
        db.query(Payment)
        .filter(Payment.id == payment_id)
        .first()
    )

    if payment:

        payment.status = "approved"
        payment.approved_at = datetime.utcnow()

        db.commit()

    return RedirectResponse(
        url="/admin/dashboard",
        status_code=303,
    )


@app.get(
    "/api/admin/approve/{payment_id}"
)
def admin_approve_payment(
    payment_id: str,
    admin_session: str = Cookie(None),
    db: Session = Depends(get_db),
):

    if admin_session != "authenticated":

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

    db.commit()

    return {
        "message": (
            f"Payment {payment_id} successfully approved."
        )
    }


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

        approve_link = (
            "http://127.0.0.1:8000"
            f"/api/admin/approve/{payment_id}"
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

@app.get(
    "/api/download/form16/{payment_id}"
)
def download_pdf(
    payment_id: str,
    db: Session = Depends(get_db),
):

    payment = (
        db.query(Payment)
        .filter(Payment.id == payment_id)
        .first()
    )

    if (
        not payment
        or payment.status != "approved"
    ):

        raise HTTPException(
            status_code=403,
            detail="Payment not approved yet.",
        )

    employee = (
        db.query(EmployeeDetail)
        .filter(
            EmployeeDetail.user_id
            == payment.user_id
        )
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
        .filter(
            EmployerCache.tan
            == employee.tan_id
        )
        .first()
    )

    if not employer:

        raise HTTPException(
            status_code=400,
            detail="Employer details are missing.",
        )

    financial_year = get_active_financial_year(db)

    # Only selected FY is allowed into Form 16.
    ledger_rows = (
        db.query(MonthlyLedger)
        .filter(
            MonthlyLedger.user_id
            == payment.user_id,

            MonthlyLedger.financial_year
            == financial_year,
        )
        .all()
    )

    if not ledger_rows:

        raise HTTPException(
            status_code=400,
            detail=(
                f"No salary ledger found for "
                f"FY {financial_year}."
            ),
        )

    # March -> February ordering.
    payroll_order = {
        3: 1,
        4: 2,
        5: 3,
        6: 4,
        7: 5,
        8: 6,
        9: 7,
        10: 8,
        11: 9,
        12: 10,
        1: 11,
        2: 12,
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

                "financial_year": (
                    row.financial_year
                ),

                "basic_pay": row.basic_pay or 0,
                "da": row.da or 0,
                "hra": row.hra or 0,

                "gross_salary": (
                    row.gross_salary or 0
                ),

                "line_items": (
                    row.line_items_json or {}
                ),

                "deductions": (
                    row.deductions_json or {}
                ),

                "source": row.source,

                "is_auto_generated": (
                    row.is_auto_generated
                ),

                "note": row.note,
                "flags": row.flags or [],
            }
        )

    if generate_form16_pdf is None:

        raise HTTPException(
            status_code=500,
            detail=(
                "PDF generator is not available."
            ),
        )

    # pdf_generator.py will be audited separately.
    # It is intentionally the only component responsible
    # for PDF layout/rendering.

    try:

        pdf_path = generate_form16_pdf(
            employee=employee,
            employer=employer,
            ledger=formatted_ledger,
            financial_year=financial_year,
        )

    except TypeError as exc:

        # Current pdf_generator may still use its old
        # function signature. We will align that file
        # in its own audit instead of silently guessing.

        raise HTTPException(
            status_code=500,
            detail=(
                "pdf_generator.py interface needs "
                f"alignment: {exc}"
            ),
        )

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=f"PDF generation failed: {exc}",
        )

    if (
        not pdf_path
        or not os.path.exists(pdf_path)
    ):

        raise HTTPException(
            status_code=500,
            detail="Generated PDF file was not found.",
        )

    safe_name = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        employee.name or "Employee",
    ).strip("_")

    filename = (
        f"Form_16_{safe_name}_FY_"
        f"{financial_year}.pdf"
    )

    return FileResponse(
        path=pdf_path,
        filename=filename,
        media_type="application/pdf",
    )
