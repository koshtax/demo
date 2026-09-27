import json
from datetime import datetime
from sqlalchemy import create_engine, Column, String, Float, Integer, Boolean, DateTime, ForeignKey, JSON
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
SQLALCHEMY_DATABASE_URL = "sqlite:///./form16_database.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker( autocommit=False, autoflush=False, bind=engine) 
Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(String, primary_key=True)
    email = Column(String, unique=True, index=True)
    mobile = Column(String, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    ledgers = relationship("MonthlyLedger", back_populates="user")
    employee_details = relationship("EmployeeDetail", back_populates="user", uselist=False)

class AdminSettings(Base):
    __tablename__ = 'admin_settings'
    id = Column(Integer, primary_key=True)
    fee_amount = Column(Float, default=150.0) # Admin editable[span_1](start_span)[span_1](end_span)
    upi_id = Column(String) # For dynamic QR[span_2](start_span)[span_2](end_span)
    telegram_bot_token = Column(String)
    telegram_chat_id = Column(String)
    tax_slabs_json = Column(JSON) # 100% dynamic tax slabs

class EmployerCache(Base):
    """TAN-based cache system - saves time for all subsequent users[span_3](start_span)[span_3](end_span)"""
    __tablename__ = 'employers_by_tan'
    tan = Column(String, primary_key=True, index=True)
    officer_name = Column(String)
    officer_father_name = Column(String)
    employer_name = Column(String)
    employer_address = Column(String)
    designation = Column(String)
    pan = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    employees = relationship("EmployeeDetail", back_populates="employer")

class EmployeeDetail(Base):
    __tablename__ = 'employee_details'
    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey('users.id'))
    tan_id = Column(String, ForeignKey('employers_by_tan.tan'))
    
    name = Column(String)
    pan = Column(String)
    office_school_name = Column(String)
    
    user = relationship("User", back_populates="employee_details")
    employer = relationship("EmployerCache", back_populates="employees")

class MonthlyLedger(Base):
    """Monthly financial data with dynamic line items[span_4](start_span)[span_4](end_span)"""
    __tablename__ = 'monthly_ledger'
    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey('users.id'))
    month = Column(Integer)
    year = Column(Integer)
    
    basic_pay = Column(Float)
    da = Column(Float)
    hra = Column(Float)
    gross_salary = Column(Float)
    
    # DYNAMIC HEADS: TA, Medical, GPF, Arrears will be stored here seamlessly
    line_items_json = Column(JSON, default={}) 
    deductions_json = Column(JSON, default={})
    
    is_auto_generated = Column(Boolean, default=False)
    source = Column(String) # 'extracted', 'combined_period', 'arrear', 'auto_generated[span_5](start_span)'[span_5](end_span)
    
    user = relationship("User", back_populates="ledgers")

class Payment(Base):
    __tablename__ = 'payments'
    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey('users.id'))
    amount = Column(Float)
    upi_txn_utr = Column(String, unique=True)
    status = Column(String, default="pending") # pending -> approved -> rejected[span_6](start_span)[span_6](end_span)
    approved_at = Column(DateTime, nullable=True)
Base.metadata.create_all(bind=engine)
# ==========================================
# 3. LIVE TESTING BLOCK (Self-Auditing)
# ==========================================
if __name__ == "__main__":
    import uuid
    # Create an in-memory SQLite database to prove the logic works instantly
    engine = create_engine('sqlite:///:memory:', echo=False)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    print("--- Running Live Tests on Dynamic Architecture ---")

    # TEST 1: TAN Cache Automation (The "Fill Once, Use Anywhere" requirement)
    tan_number = "RNCEDNK35" # From Jaya Kumari's slip[span_7](start_span)[span_7](end_span)
    
    # Simulate User 1 entering TAN details manually
    new_employer = EmployerCache(
        tan=tan_number,
        officer_name="Ramesh Kumar",
        employer_name="SDEO KHUNTI",
        designation="SDEO"
    )
    session.add(new_employer)
    session.commit()
    print(f"✓ User 1 registered TAN: {tan_number}")

    # Simulate User 2 trying to fetch the same TAN
    cached_employer = session.query(EmployerCache).filter_by(tan=tan_number).first()
    if cached_employer:
        print(f"✓ User 2 auto-fetched data: {cached_employer.employer_name} ({cached_employer.designation})")

    # TEST 2: Dynamic JSON Storage (Handling unexpected allowances without crashing)
    user_id = str(uuid.uuid4())
    session.add(User(id=user_id, email="test@test.com", mobile="9999999999"))
    
    # Inserting Jaya Kumari's June-Oct combined slip data dynamically
    ledger_entry = MonthlyLedger(
        id=str(uuid.uuid4()),
        user_id=user_id,
        month=10,
        year=2025,
        basic_pay=91540.0,  # Combined basic[span_8](start_span)[span_8](end_span)
        da=52735.0,
        hra=9154.0,
        gross_salary=155729.0,
        source="combined_period",
        # Everything else goes into dynamic JSON!
        line_items_json={"medical_allowance": 2300.0, "special_bonus": 0.0},
        deductions_json={"gli": 150.0, "gpf": 6000.0, "tax": 0.0}
    )
    session.add(ledger_entry)
    session.commit()
    
    # Fetch and verify dynamic data
    saved_ledger = session.query(MonthlyLedger).filter_by(user_id=user_id).first()
    print(f"✓ Dynamic JSON saved successfully: Medical = ₹{saved_ledger.line_items_json.get('medical_allowance')}")
    print(f"✓ Source tracked as: '{saved_ledger.source}' (Matrix bypass flag active)")
