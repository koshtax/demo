from datetime import datetime

from sqlalchemy import (
    create_engine,
    Column,
    String,
    Float,
    Integer,
    Boolean,
    DateTime,
    ForeignKey,
    JSON,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship


SQLALCHEMY_DATABASE_URL = "sqlite:///./form16_database.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True)
    email = Column(String, unique=True, index=True, nullable=True)
    mobile = Column(String, unique=True, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    ledgers = relationship(
        "MonthlyLedger",
        back_populates="user",
        cascade="all, delete-orphan",
    )

    employee_details = relationship(
        "EmployeeDetail",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )


class AdminSettings(Base):
    __tablename__ = "admin_settings"

    id = Column(Integer, primary_key=True)

    fee_amount = Column(Float, default=150.0)
    upi_id = Column(String)

    telegram_bot_token = Column(String)
    telegram_chat_id = Column(String)

    # Admin-selected March -> February payroll/Form-16 cycle.
    # Example: "2026-27"
    financial_year = Column(String)

    # Tax configuration can later be loaded dynamically.
    tax_slabs_json = Column(JSON, nullable=True)


class EmployerCache(Base):
    """
    TAN-based employer/DDO cache.
    Once saved, the same TAN can auto-fill employer details for later users.
    """

    __tablename__ = "employers_by_tan"

    tan = Column(String, primary_key=True, index=True)

    officer_name = Column(String)
    officer_father_name = Column(String)

    employer_name = Column(String)
    employer_address = Column(String)
    designation = Column(String)
    pan = Column(String, nullable=True)

    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    employees = relationship(
        "EmployeeDetail",
        back_populates="employer",
    )


class EmployeeDetail(Base):
    __tablename__ = "employee_details"

    id = Column(String, primary_key=True)

    user_id = Column(
        String,
        ForeignKey("users.id"),
        unique=True,
        nullable=False,
    )

    tan_id = Column(
        String,
        ForeignKey("employers_by_tan.tan"),
        nullable=True,
    )

    name = Column(String)
    pan = Column(String)
    office_school_name = Column(String)

    user = relationship(
        "User",
        back_populates="employee_details",
    )

    employer = relationship(
        "EmployerCache",
        back_populates="employees",
    )


class MonthlyLedger(Base):
    """
    One payroll-ledger row for one exact month/year.

    Dynamic earning heads such as TA, Medical and other allowances are stored
    in line_items_json. Dynamic deductions are stored in deductions_json.
    """

    __tablename__ = "monthly_ledger"

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "month_year",
            name="uq_monthly_ledger_user_month_year",
        ),
    )

    id = Column(String, primary_key=True)

    user_id = Column(
        String,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    month = Column(Integer, nullable=False)
    year = Column(Integer, nullable=False)

    # Canonical exact month identity, e.g. "2026-01".
    month_year = Column(String, nullable=False)

    # Admin-selected March-February cycle, e.g. "2025-26".
    financial_year = Column(String, nullable=False, index=True)

    basic_pay = Column(Float, default=0.0)
    da = Column(Float, default=0.0)
    hra = Column(Float, default=0.0)
    gross_salary = Column(Float, default=0.0)

    # TA, Medical, special allowance, arrear heads, etc.
    line_items_json = Column(JSON, default=dict)

    # GPF, GLI, professional tax, TDS and other deductions.
    deductions_json = Column(JSON, default=dict)

    is_auto_generated = Column(Boolean, default=False)

    # Typical values:
    # extracted / combined_period / arrear / auto_generated
    source = Column(String, nullable=False, default="extracted")

    # Preserve projection/manual-review audit information.
    note = Column(String, nullable=True)
    flags = Column(JSON, default=list)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    user = relationship(
        "User",
        back_populates="ledgers",
    )


class Payment(Base):
    __tablename__ = "payments"

    id = Column(String, primary_key=True)

    user_id = Column(
        String,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    amount = Column(Float, nullable=False)
    upi_txn_utr = Column(String, unique=True, index=True, nullable=False)

    # pending -> approved -> rejected
    status = Column(String, default="pending")

    approved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)


# ==========================================
# LIVE TESTING BLOCK
# ==========================================
if __name__ == "__main__":
    import uuid

    test_engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
    )

    Base.metadata.create_all(test_engine)

    TestSession = sessionmaker(bind=test_engine)
    session = TestSession()

    print("--- Running Live Tests on Database Architecture ---")

    # TEST 1: TAN cache, including officer father name.
    tan_number = "RNCEDNK35"

    new_employer = EmployerCache(
        tan=tan_number,
        officer_name="Ramesh Kumar",
        officer_father_name="Test Father Name",
        employer_name="SDEO KHUNTI",
        employer_address="Khunti, Jharkhand",
        designation="SDEO",
    )

    session.add(new_employer)
    session.commit()

    cached_employer = (
        session.query(EmployerCache)
        .filter_by(tan=tan_number)
        .first()
    )

    print(
        "✓ TAN cache:",
        cached_employer.employer_name,
        cached_employer.officer_father_name,
    )

    # TEST 2: Exact month/year ledger identity + dynamic Medical storage.
    user_id = str(uuid.uuid4())

    session.add(
        User(
            id=user_id,
            email="test@test.com",
            mobile="9999999999",
        )
    )

    session.commit()

    ledger_entry = MonthlyLedger(
        id=str(uuid.uuid4()),
        user_id=user_id,
        month=10,
        year=2025,
        month_year="2025-10",
        financial_year="2025-26",
        basic_pay=91540.0,
        da=52735.0,
        hra=9154.0,
        gross_salary=155729.0,
        source="combined_period",
        line_items_json={
            "medical": 2300.0,
            "special_bonus": 0.0,
        },
        deductions_json={
            "gli": 150.0,
            "gpf": 6000.0,
            "tds": 0.0,
        },
        flags=[
            "Combined-period bill requires review before regular-month use."
        ],
    )

    session.add(ledger_entry)
    session.commit()

    saved_ledger = (
        session.query(MonthlyLedger)
        .filter_by(
            user_id=user_id,
            month_year="2025-10",
        )
        .first()
    )

    print(
        "✓ Month/year:",
        saved_ledger.month_year,
        "| FY:",
        saved_ledger.financial_year,
    )

    print(
        "✓ Medical:",
        saved_ledger.line_items_json.get("medical"),
    )

    print(
        "✓ Source:",
        saved_ledger.source,
    )

    session.close()
