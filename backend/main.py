import uuid
import requests
from datetime import datetime
from typing import Optional, Dict, Any

# Required FastAPI Imports for APIs, UI Rendering, and Background Tasks
from fastapi import FastAPI, Depends, HTTPException, status, Request, BackgroundTasks, File, UploadFile
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from fastapi import Request, Form, Response, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException

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
    # Updated syntax for newer FastAPI versions
    return templates.TemplateResponse(request=request, name="upload.html")

@app.get("/review")
def review_page(request: Request, user_id: str = ""):
    return templates.TemplateResponse(
        request=request, 
        name="review.html", 
        context={"user_id": user_id}
    )

@app.get("/ddo-details")
def ddo_details_page(request: Request, user_id: str = ""):
    return templates.TemplateResponse(
        request=request, 
        name="ddo_details.html", 
        context={"user_id": user_id}
    )

@app.get("/payment")
def payment_page(request: Request, user_id: str = "1", db: Session = Depends(get_db)):
    # Dynamically fetch admin details for the QR code
    settings = db.query(AdminSettings).first()
    upi_id = settings.upi_id if settings else "admin@ybl"
    fee_amount = settings.fee_amount if settings else 150.0
    
    return templates.TemplateResponse(
        request=request, 
        name="payment.html", 
        context={
            "upi_id": upi_id,
            "fee_amount": fee_amount,
            "user_id": user_id  # Ab ye crash nahi hoga
        }
    )
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

import time

# Upar imports me ye line add karo (apne actual parser function ke hisab se naam change kar lena)
from core.parser import load_and_parse_pdf 
import os
import tempfile
from fastapi import File, UploadFile, HTTPException
from core.parser import load_and_parse_pdf

@app.post("/api/extract")
async def extract_salary_slip(file: UploadFile = File(...)):
    """Real PDF file receive karta hai aur core/parser.py ko bhejta hai"""
    if not file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are allowed")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        # Asli parser function call ho raha hai
        parsed_result = load_and_parse_pdf(tmp_path)
        
        # Check agar PDF scan copy thi ya extract nahi ho payi
        if not parsed_result.get("success"):
            raise HTTPException(status_code=400, detail=parsed_result.get("error", "Failed to parse PDF"))
            
        # Extraction successful
        extracted_slips = parsed_result.get("slips", [])
        return {"message": "Extracted successfully", "data": extracted_slips}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Parsing Error: {str(e)}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

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
# ADMIN PANEL ROUTES (UPDATED FIX)
# ==========================================

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, admin_session: str = Cookie(None)):
    if admin_session == "authenticated":
        return RedirectResponse(url="/admin/dashboard")
    # FIX: Added explicitly named parameters (request=, name=, context=)
    return templates.TemplateResponse(request=request, name="admin_login.html", context={"request": request, "error": None})


@app.post("/admin/login")
async def admin_login(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == "admin_27" and password == "@admin_def27":
        response = RedirectResponse(url="/admin/dashboard", status_code=303)
        response.set_cookie(key="admin_session", value="authenticated", httponly=True)
        return response
    else:
        return templates.TemplateResponse(request=request, name="admin_login.html", context={"request": request, "error": "Galat ID ya Password!"})

@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request, admin_session: str = Cookie(None), db: Session = Depends(get_db)):
    # Bina login ke access block karo
    if admin_session != "authenticated":
        return RedirectResponse(url="/admin", status_code=303)
    
    # Database se saari payments nikalna
    payments = db.query(Payment).order_by(Payment.status.desc()).all() 
    
    # HTML ke liye dono tables (Payment + Employee) ka data mix karna
    dashboard_data = []
    for p in payments:
        emp = db.query(EmployeeDetail).filter(EmployeeDetail.user_id == p.user_id).first()
        
        dashboard_data.append({
            "payment_id": p.id,
            "id": p.id, # Fallback ID
            "name": emp.name if emp else "Unknown Employee",
            "pan": emp.pan if emp else "N/A",
            "utr_number": p.upi_txn_utr,  # HTML format mapping
            "amount": p.amount,
            "status": p.status
        })
        
    # Nayi payments sabse upar dikhane ke liye list ko reverse kar do
    dashboard_data.reverse()
    
    # "users" aur "payments" dono keys bhej rahe hain taaki HTML error na de
    return templates.TemplateResponse(
        request=request, 
        name="admin_dashboard.html", 
        context={"request": request, "users": dashboard_data, "payments": dashboard_data}
    )


@app.get("/admin/logout")
async def admin_logout():
    response = RedirectResponse(url="/admin", status_code=303)
    response.delete_cookie("admin_session")
    return response

import os
from fastapi.responses import FileResponse

@app.get("/api/download-pdf")
async def download_form16():
    # Abhi testing ke liye hum ek temporary file bana rahe hain
    # (Baad me yahan tumhara pdf_generator.py actual data se PDF banayega)
    pdf_path = "Form_16_Generated.pdf"
    
    # Agar file nahi hai, toh ek dummy file create kar lo test ke liye
    if not os.path.exists(pdf_path):
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF-1.4\n%Dummy PDF for testing\n")
            
    # FileResponse browser ko direct download prompt deta hai
    return FileResponse(
        path=pdf_path, 
        filename="Form_16_Nitin_Mallick_FY25-26.pdf", 
        media_type="application/pdf"
    )

@app.post("/admin/payment/approve/{payment_id}")
def approve_payment(payment_id: str, request: Request, admin_session: str = Cookie(None), db: Session = Depends(get_db)):
    # Security check
    if admin_session != "authenticated":
        return RedirectResponse(url="/admin", status_code=303)
    
    # Database me status update karo
    payment = db.query(Payment).filter(Payment.id == payment_id).first()
    if payment:
        payment.status = "approved"
        db.commit()
    
    return RedirectResponse(url="/admin/dashboard", status_code=303)

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

from fastapi import Request # Upar check karlena ki ye import ho

@app.post("/api/payment/submit")
async def submit_utr(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    data = await request.json()
    
    user_id = data.get("user_id")
    utr_number = data.get("utr_number")
    amount = data.get("amount")
    slips = data.get("slips", [])
    
    try:
        # 🟢 FIX 1: Sabse pehle Base User banayein taaki Foreign Key (Integrity) Error na aaye
        base_user = db.query(User).filter(User.id == user_id).first()
        if not base_user:
            base_user = User(id=user_id)
            db.add(base_user)
            db.flush() # Database me turant ID register karne ke liye
            
        payment_id = str(uuid.uuid4())
        new_payment = Payment(
            id=payment_id,
            user_id=user_id,
            amount=amount,
            upi_txn_utr=utr_number,
            status="pending"
        )
        db.add(new_payment)
        
        if slips:
            main_slip = slips[0]
            pan_no = main_slip.get("pan_no", "UNKNOWN")
            
            all_employers = db.query(EmployerCache).all()
            fallback_tan = all_employers[-1].tan if all_employers else "RNCEDNK91"

            user_detail = db.query(EmployeeDetail).filter(EmployeeDetail.user_id == user_id).first()
            if not user_detail:
                user_detail = EmployeeDetail(id=str(uuid.uuid4()), user_id=user_id)
                db.add(user_detail)
            
                
            user_detail.pan = pan_no
            user_detail.tan_id = fallback_tan 
            user_detail.name = main_slip.get("employee_name", "Employee")
            
            db.query(MonthlyLedger).filter(MonthlyLedger.user_id == user_id).delete()
            
            for slip in slips:
                line_items = slip.get("line_items", {})
                deductions = slip.get("deductions", {})
                period = slip.get("period", {})
                
                month_val = 1
                year_val = 2025
                if "months" in period and len(period["months"]) > 0:
                    month_val = period["months"][0][0]
                    year_val = period["months"][0][1]
                
                new_ledger = MonthlyLedger(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    month=month_val,
                    year=year_val,
                    basic_pay=line_items.get("basic_pay", 0),
                    da=line_items.get("da", 0),
                    hra=line_items.get("hra", 0),
                    gross_salary=slip.get("gross_salary", 0),
                    deductions_json={
                        "gpf": deductions.get("gpf", 0),
                        "gli": deductions.get("gli", 0),
                        "prof_tax": deductions.get("prof_tax", 0),
                        "tds": deductions.get("tds", 0)
                    }
                )
                db.add(new_ledger)

        db.commit()
        background_tasks.add_task(send_telegram_notification, db, payment_id, utr_number, user_id)
        return {"message": "UTR Submitted successfully", "payment_id": payment_id}
        
    except IntegrityError as e:
        db.rollback()
        # 🟢 FIX 2: Ab ye jhooth nahi bolega, asali DB error screen par dikhayega!
        error_msg = str(e.orig)
        if "UNIQUE" in error_msg.upper() and "UTR" in error_msg.upper():
            raise HTTPException(status_code=400, detail="Ye UTR number sach me pehle se use ho chuka hai.")
        else:
            # Agar error kuch aur hoga toh screen par exact beemari ka naam aayega
            raise HTTPException(status_code=400, detail=f"Database Crash (Real Error): {error_msg}")
            
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Server Error: {str(e)}")

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

    # 🟢 Asali calculations + SMART AUTO-FILL (Increment Logic)
    formatted_ledger = []
    total_basic = total_da = total_hra = gross_salary = 0
    total_gpf = total_gli = total_tds = total_prof_tax = 0

    month_dict = {
        1: "January", 2: "February", 3: "March", 4: "April", 
        5: "May", 6: "June", 7: "July", 8: "August", 
        9: "September", 10: "October", 11: "November", 12: "December"
    }

    fy_months = [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 1, 2]
    ledger_dict = {l.month: l for l in ledger}
    
    # 🔥 FIX: June aur July ka Basic compare karo
    june_basic = ledger_dict[6].basic_pay if 6 in ledger_dict else 0
    july_basic = ledger_dict[7].basic_pay if 7 in ledger_dict else 0
    
    # Agar June aur July barabar hain, toh Jan me increment pakka hai
    apply_jan_increment = False
    if june_basic > 0 and july_basic > 0 and june_basic == july_basic:
        apply_jan_increment = True

    last_known = {"basic": 0, "da": 0, "hra": 0, "gpf": 0, "gli": 0, "prof_tax": 0, "tds": 0}

    for m in fy_months:
        if m in ledger_dict:
            # Data pehle se database me hai
            l = ledger_dict[m]
            basic = l.basic_pay
            da = l.da
            hra = l.hra
            gross = l.gross_salary
            
            gpf_val = l.deductions_json.get("gpf", 0) if l.deductions_json else 0
            gli_val = l.deductions_json.get("gli", 0) if l.deductions_json else 0
            prof_tax_val = l.deductions_json.get("prof_tax", 0) if l.deductions_json else 0
            tds_val = l.deductions_json.get("tds", 0) if l.deductions_json else 0
            
            last_known.update({
                "basic": basic, "da": da, "hra": hra, 
                "gpf": gpf_val, "gli": gli_val, "prof_tax": prof_tax_val, "tds": tds_val
            })
            month_label = month_dict[m]
            
        else:
            # 🟢 AUTO-FILL LOGIC with Increment
            if last_known["basic"] == 0:
                continue 
                
            # Agar January (1) hai aur increment lagna hai
            if m == 1 and apply_jan_increment:
                # 3% increment rule, rounded to nearest 100
                new_basic = round((last_known["basic"] * 1.03) / 100) * 100
                
                # Naye basic par purana DA percentage lagao
                da_percent = last_known["da"] / last_known["basic"] if last_known["basic"] > 0 else 0
                new_da = round(new_basic * da_percent)
                
                last_known["basic"] = new_basic
                last_known["da"] = new_da
                
            basic = last_known["basic"]
            da = last_known["da"]
            hra = last_known["hra"]
            gpf_val = last_known["gpf"]
            gli_val = last_known["gli"]
            prof_tax_val = last_known["prof_tax"]
            tds_val = last_known["tds"]
            gross = basic + da + hra
            
            month_label = f"{month_dict[m]} (Auto)"

        # Data PDF ke liye format karna
        formatted_ledger.append({
            "month_name": month_label, 
            "basic": f"{basic:.2f}", "da": f"{da:.2f}", "hra": f"{hra:.2f}",
            "gross": f"{gross:.2f}", "gpf": f"{gpf_val:.2f}", "gli": f"{gli_val:.2f}",
            "prof_tax": f"{prof_tax_val:.2f}", "tds": f"{tds_val:.2f}",
            "total_deduction": f"{(gpf_val + gli_val + prof_tax_val + tds_val):.2f}",
            "net_pay": f"{(gross - (gpf_val + gli_val + prof_tax_val + tds_val)):.2f}"
        })
        
        # Totals update karna
        total_basic += basic
        total_da += da
        total_hra += hra
        gross_salary += gross
        total_gpf += gpf_val
        total_gli += gli_val
        total_tds += tds_val
        total_prof_tax += prof_tax_val

    if not generate_form16_pdf:
        raise HTTPException(status_code=500, detail="PDF Generator module missing. Check pdf_generator.py")
        
    user.designation = getattr(user, 'designation', 'CLERK')
    user.office_school_name = getattr(user, 'office_school_name', 'Utkramit +2 High School, Tubil')

    # 🟢 Saara data ek bundle me pack karo
    tax_data_bundle = {
        "salary_amount": f"{total_basic:.2f}",
        "da_amount": f"{total_da:.2f}",
        "hra_amount": f"{total_hra:.2f}",
        "gross_total_income": f"{gross_salary:.2f}",
        "taxable_income": f"{(gross_salary - 75000):.2f}",
        "standard_deduction": "75000.00",
        
        "q1_tds": f"{total_tds:.2f}",
        "total_tds": f"{total_tds:.2f}",
        "challan_tax": f"{total_tds:.2f}",
        "tds_paid": f"{total_tds:.2f}",
        
        "total_gpf": f"{total_gpf:.2f}",
        "total_gis": f"{total_gli:.2f}",
        "total_80c": f"{(total_gpf + total_gli):.2f}",
        "total_80c_deductible": f"{(total_gpf + total_gli):.2f}",
        
        "total_prof_tax": f"{total_prof_tax:.2f}",
        "total_deductions_sum": f"{(total_gpf + total_gli + total_tds + total_prof_tax):.2f}",
        "net_pay_sum": f"{(gross_salary - (total_gpf + total_gli + total_tds + total_prof_tax)):.2f}",
        "da_arrears": 0
    }

    pdf_path = generate_form16_pdf(
        user_data=user, 
        ledger_data=formatted_ledger, 
        tax_data=tax_data_bundle, 
        employer_data=employer
    )
    
    return FileResponse(path=pdf_path, filename=f"Form16_{user.pan}.pdf", media_type='application/pdf')

