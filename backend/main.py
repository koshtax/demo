import uuid
import requests
from datetime import datetime
from typing import Optional, Dict, Any

# Required FastAPI Imports for APIs, UI Rendering, and Background Tasks
from fastapi import FastAPI, Depends, HTTPException, status, Request, BackgroundTasks
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

# 1. Tumhare Models Import (Ensure models.py is in the same folder)
from models import SessionLocal, EmployerCache, MonthlyLedger, User, AdminSettings, Payment, EmployeeDetail

# 2. PDF Generator Import (Assumes pdf_generator.py is in the same folder)
try:
    from pdf_generator import generate_form16_pdf
except ImportError:
    # App crash se bachane ke liye safe-fail
    generate_form16_pdf = None

app = FastAPI(title="Form 16 Generator API", version="1.0")

# Jinja2 Templates Directory Configuration
templates = Jinja2Templates(directory="templates")

# Database Injection Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ==========================================
# PHASE 1: PYDANTIC VALIDATION SCHEMAS
# ==========================================
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
    basic_pay: float
    da: float
    hra: float
    gross_salary: float
    source: str
    line_items: Dict[str, float] = {}
    deductions: Dict[str, float] = {}

class PaymentSubmitSchema(BaseModel):
    user_id: str
    utr_number: str
    amount: float


# ==========================================
# PHASE 2: UI RENDERING ROUTES (HTML PAGES)
# ==========================================
@app.get("/")
def read_root(request: Request):
    return templates.TemplateResponse("upload.html", {"request": request})

@app.get("/review")
def review_page(request: Request):
    return templates.TemplateResponse("review.html", {"request": request})

@app.get("/ddo-details")
def ddo_details_page(request: Request):
    return templates.TemplateResponse("ddo_details.html", {"request": request})

@app.get("/payment")
def payment_page(request: Request, db: Session = Depends(get_db)):
    # Dynamically fetch admin details for the QR code
    settings = db.query(AdminSettings).first()
    upi_id = settings.upi_id if settings else "admin@ybl"
    fee_amount = settings.fee_amount if settings else 150.0
    
    return templates.TemplateResponse("payment.html", {
        "request": request,
        "upi_id": upi_id,
        "fee_amount": fee_amount
    })


# ==========================================
# PHASE 3: CORE DATABASE API (TAN & LEDGER)
# ==========================================
@app.get("/api/employer/{tan}", response_model=EmployerSchema)
def get_employer_by_tan(tan: str, db: Session = Depends(get_db)):
    employer = db.query(EmployerCache).filter(EmployerCache.tan == tan).first()
    if not employer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="TAN not found. User needs to enter manually."
        )
    return employer

@app.post("/api/employer", response_model=EmployerSchema)
def upsert_employer(emp_data: EmployerSchema, db: Session = Depends(get_db)):
    employer = db.query(EmployerCache).filter(EmployerCache.tan == emp_data.tan).first()
    if employer:
        employer.officer_name = emp_data.officer_name
        employer.officer_father_name = emp_data.officer_father_name
        employer.employer_name = emp_data.employer_name
        employer.employer_address = emp_data.employer_address
        employer.designation = emp_data.designation
        employer.pan = emp_data.pan
    else:
        employer = EmployerCache(**emp_data.dict())
        db.add(employer)
    db.commit()
    db.refresh(employer)
    return employer

@app.post("/api/ledger/save", status_code=status.HTTP_201_CREATED)
def save_monthly_ledger(ledger_data: LedgerSchema, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == ledger_data.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    existing_ledger = db.query(MonthlyLedger).filter(
        MonthlyLedger.user_id == ledger_data.user_id,
        MonthlyLedger.month == ledger_data.month,
        MonthlyLedger.year == ledger_data.year
    ).first()

    if existing_ledger:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail=f"Ledger entry for {ledger_data.month}/{ledger_data.year} already exists."
        )

    new_ledger = MonthlyLedger(
        id=str(uuid.uuid4()),
        user_id=ledger_data.user_id,
        month=ledger_data.month,
        year=ledger_data.year,
        basic_pay=ledger_data.basic_pay,
        da=ledger_data.da,
        hra=ledger_data.hra,
        gross_salary=ledger_data.gross_salary,
        source=ledger_data.source,
        is_auto_generated=(ledger_data.source == "auto_generated"),
        line_items_json=ledger_data.line_items,
        deductions_json=ledger_data.deductions
    )
    db.add(new_ledger)
    db.commit()
    db.refresh(new_ledger)
    return {"message": "Salary data stored successfully", "ledger_id": new_ledger.id}


# ==========================================
# PHASE 4: PAYMENT & TELEGRAM APPROVAL FLOW
# ==========================================
def send_telegram_notification(db: Session, payment_id: str, utr: str, user_id: str):
    """Sends UTR to Admin via Telegram. Runs in background to prevent UI lag."""
    admin_settings = db.query(AdminSettings).first()
    if not admin_settings or not admin_settings.telegram_bot_token:
        return 
        
    employee = db.query(EmployeeDetail).filter(EmployeeDetail.user_id == user_id).first()
    emp_name = employee.name if employee else "Unknown User"
    emp_pan = employee.pan if employee else "Unknown PAN"
    
    approve_link = f"http://127.0.0.1:8000/api/admin/approve/{payment_id}"
    msg = (
        f"🚨 *New Form 16 Payment Request*\n\n"
        f"👤 *Name:* {emp_name}\n"
        f"💳 *PAN:* {emp_pan}\n"
        f"🏦 *UTR No:* `{utr}`\n\n"
        f"✅ [Click Here to Approve]({approve_link})"
    )
    
    url = f"https://api.telegram.org/bot{admin_settings.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": admin_settings.telegram_chat_id,
        "text": msg,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    try:
        # Added try-except so the API doesn't crash if Telegram is down
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Telegram Notification Failed: {e}")

@app.post("/api/payment/submit")
def submit_utr(data: PaymentSubmitSchema, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    payment_id = str(uuid.uuid4())
    new_payment = Payment(
        id=payment_id,
        user_id=data.user_id,
        amount=data.amount,
        upi_txn_utr=data.utr_number,
        status="pending"
    )
    db.add(new_payment)
    db.commit()
    
    # Background task triggered here
    background_tasks.add_task(send_telegram_notification, db, payment_id, data.utr_number, data.user_id)
    return {"message": "UTR Submitted successfully", "payment_id": payment_id}

@app.get("/api/payment/status/{payment_id}")
def check_payment_status(payment_id: str, db: Session = Depends(get_db)):
    payment = db.query(Payment).filter(Payment.id == payment_id).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    return {"status": payment.status}

@app.get("/api/admin/approve/{payment_id}")
def admin_approve_payment(payment_id: str, db: Session = Depends(get_db)):
    payment = db.query(Payment).filter(Payment.id == payment_id).first()
    if payment:
        payment.status = "approved"
        payment.approved_at = datetime.utcnow()
        db.commit()
        return {"message": f"Payment {payment_id} successfully APPROVED! User can now download Form 16."}
    return {"message": "Invalid Payment ID"}


# ==========================================
# PHASE 5: FINAL PDF GENERATOR ROUTE
# ==========================================
@app.get("/api/download/form16/{payment_id}")
def download_pdf(payment_id: str, db: Session = Depends(get_db)):
    payment = db.query(Payment).filter(Payment.id == payment_id).first()
    if not payment or payment.status != "approved":
        raise HTTPException(status_code=403, detail="Payment not approved yet.")
        
    user = db.query(EmployeeDetail).filter(EmployeeDetail.user_id == payment.user_id).first()
    ledger = db.query(MonthlyLedger).filter(MonthlyLedger.user_id == payment.user_id).order_by(MonthlyLedger.month).all()
    
    if not user or not user.tan_id:
        raise HTTPException(status_code=400, detail="Incomplete user or employer details.")
        
    employer = db.query(EmployerCache).filter(EmployerCache.tan == user.tan_id).first()
    
    # Extracting exact data for WeasyPrint
    formatted_ledger = []
    total_basic = 0
    for l in ledger:
        # Dynamic handling for JSON fields to avoid KeyErrors
        gpf_val = l.deductions_json.get("gpf", 0) if l.deductions_json else 0
        gli_val = l.deductions_json.get("gli", 0) if l.deductions_json else 0
        prof_tax_val = l.deductions_json.get("prof_tax", 0) if l.deductions_json else 0
        tds_val = l.deductions_json.get("tds", 0) if l.deductions_json else 0
        
        formatted_ledger.append({
            "month_name": l.month,
            "basic": l.basic_pay,
            "da": l.da,
            "hra": l.hra,
            "gross": l.gross_salary,
            "gpf": gpf_val,
            "gli": gli_val,
            "prof_tax": prof_tax_val,
            "tds": tds_val,
            "total_deduction": gpf_val + gli_val + prof_tax_val + tds_val,
            "net_pay": l.gross_salary - (gpf_val + gli_val + prof_tax_val + tds_val)
        })
        total_basic += l.basic_pay
        
    if not generate_form16_pdf:
        raise HTTPException(status_code=500, detail="PDF Generator module missing. Check pdf_generator.py")
        
    pdf_path = generate_form16_pdf(
        user_data=user, 
        ledger_data=formatted_ledger, 
        tax_data={"total_basic": total_basic, "gross_salary": sum(l.gross_salary for l in ledger)}, 
        employer_data=employer
    )
    
    return FileResponse(path=pdf_path, filename=f"Form16_{user.pan}.pdf", media_type='application/pdf')
