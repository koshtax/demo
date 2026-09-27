from fastapi import FastAPI, Depends, HTTPException, status
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from datetime import datetime
import uuid

# Tumhare models.py se imports
from models import SessionLocal, EmployerCache, MonthlyLedger, User

app = FastAPI(title="Form 16 Generator API", version="1.0")

# Dependency: Database session inject karne ke liye
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ==========================================
# 1. Pydantic Validation Schemas
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
    # Dynamic dictionaries jo DB me JSON banenge
    line_items: Dict[str, float] = {}
    deductions: Dict[str, float] = {}

# ==========================================
# 2. Employer / TAN Cache API Routes
# ==========================================

@app.get("/api/employer/{tan}", response_model=EmployerSchema)
def get_employer_by_tan(tan: str, db: Session = Depends(get_db)):
    """TAN cache se DDO details fetch karta hai (Auto-fill ke liye)."""
    employer = db.query(EmployerCache).filter(EmployerCache.tan == tan).first()
    if not employer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="TAN not found. User needs to enter manually."
        )
    return employer

@app.post("/api/employer", response_model=EmployerSchema)
def upsert_employer(emp_data: EmployerSchema, db: Session = Depends(get_db)):
    """Naya TAN save karta hai, ya purana data edit hone par update karta hai."""
    employer = db.query(EmployerCache).filter(EmployerCache.tan == emp_data.tan).first()
    
    if employer:
        # Update existing record
        employer.officer_name = emp_data.officer_name
        employer.officer_father_name = emp_data.officer_father_name
        employer.employer_name = emp_data.employer_name
        employer.employer_address = emp_data.employer_address
        employer.designation = emp_data.designation
        employer.pan = emp_data.pan
    else:
        # Insert new record
        employer = EmployerCache(**emp_data.dict())
        db.add(employer)
        
    db.commit()
    db.refresh(employer)
    return employer

# ==========================================
# 3. Ledger / Salary Data API Routes
# ==========================================

@app.post("/api/ledger/save", status_code=status.HTTP_201_CREATED)
def save_monthly_ledger(ledger_data: LedgerSchema, db: Session = Depends(get_db)):
    """OCR se extracted ya auto-generated January ledger ko DB me save karta hai."""
    
    # Check if user exists (Foreign Key constraint)
    user = db.query(User).filter(User.id == ledger_data.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    # Check for existing entry to prevent duplicates for the same month/year
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

