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

    # Contact details are stored here permanently.
    # The user-flow pages/main.py will be updated later to actually persist them.
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

    payments = relationship(
        "Payment",
        back_populates="user",
    )

    visitor_sessions = relationship(
        "VisitorSession",
        back_populates="user",
    )

    form16_generations = relationship(
        "Form16Generation",
        back_populates="user",
    )

    email_deliveries = relationship(
        "EmailDelivery",
        back_populates="user",
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

    user = relationship(
        "User",
        back_populates="payments",
    )

    form16_generations = relationship(
        "Form16Generation",
        back_populates="payment",
    )

    email_deliveries = relationship(
        "EmailDelivery",
        back_populates="payment",
    )


class VisitorSession(Base):
    """
    Tracks one user journey through the Form-16 flow.

    visitor_id is the stable browser-level identifier used for unique-visitor
    analytics. id is one specific application/session journey. A returning
    browser can therefore have multiple journeys without inflating the unique
    visitor count.
    """

    __tablename__ = "visitor_sessions"

    id = Column(String, primary_key=True)

    visitor_id = Column(
        String,
        nullable=False,
        index=True,
    )

    user_id = Column(
        String,
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )

    # Captured as soon as the user supplies contact information, even if the
    # application is abandoned before the full EmployeeDetail is completed.
    email = Column(String, nullable=True, index=True)
    mobile = Column(String, nullable=True)

    # Examples:
    # upload / review / ddo_details / payment / payment_wait / completed
    current_page = Column(String, nullable=True, index=True)
    current_step = Column(String, nullable=True)
    last_completed_step = Column(String, nullable=True)
    progress_percent = Column(Integer, default=0)

    # in_progress / payment_pending / payment_submitted / completed / abandoned
    application_status = Column(
        String,
        nullable=False,
        default="in_progress",
        index=True,
    )

    # not_started / pending / approved / rejected
    payment_status = Column(
        String,
        nullable=False,
        default="not_started",
        index=True,
    )

    # Only a whitelisted partial snapshot should be stored by main.py.
    # Never place passwords, raw PDF bytes, card details or UPI PINs here.
    form_snapshot_json = Column(JSON, default=dict)

    # Used for a secure resume link. main.py will generate a random token.
    resume_token = Column(
        String,
        unique=True,
        nullable=True,
        index=True,
    )

    # Service-email/reminder consent captured in the UI.
    email_contact_consent = Column(Boolean, default=False)

    # not_eligible / pending / sent / failed / cancelled
    reminder_status = Column(
        String,
        nullable=False,
        default="not_eligible",
        index=True,
    )

    reminder_due_at = Column(DateTime, nullable=True, index=True)
    reminder_sent_at = Column(DateTime, nullable=True)

    started_at = Column(DateTime, default=datetime.utcnow, index=True)
    last_seen_at = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at = Column(DateTime, nullable=True)
    abandoned_at = Column(DateTime, nullable=True)

    user = relationship(
        "User",
        back_populates="visitor_sessions",
    )

    events = relationship(
        "VisitorEvent",
        back_populates="visitor_session",
        cascade="all, delete-orphan",
    )

    email_deliveries = relationship(
        "EmailDelivery",
        back_populates="visitor_session",
    )


class VisitorEvent(Base):
    """
    Append-only funnel/event history for analytics.

    Typical event_type values:
    page_view / form_started / contact_saved / step_completed /
    payment_submitted / payment_approved / payment_rejected /
    form16_generated / form16_downloaded
    """

    __tablename__ = "visitor_events"

    id = Column(String, primary_key=True)

    session_id = Column(
        String,
        ForeignKey("visitor_sessions.id"),
        nullable=False,
        index=True,
    )

    # Denormalised for fast unique-visitor/funnel queries.
    visitor_id = Column(String, nullable=False, index=True)

    user_id = Column(
        String,
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )

    event_type = Column(String, nullable=False, index=True)
    page_name = Column(String, nullable=True, index=True)
    step_name = Column(String, nullable=True)

    # Keep this metadata minimal and non-sensitive.
    event_data_json = Column(JSON, default=dict)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    visitor_session = relationship(
        "VisitorSession",
        back_populates="events",
    )


class Form16Generation(Base):
    """
    Permanent audit record for every Form 16 generation.

    source distinguishes normal paid-user generation from direct admin
    generation, so admin-generated documents never need fake payment records.
    """

    __tablename__ = "form16_generations"

    id = Column(String, primary_key=True)

    user_id = Column(
        String,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    payment_id = Column(
        String,
        ForeignKey("payments.id"),
        nullable=True,
        index=True,
    )

    financial_year = Column(String, nullable=False, index=True)
    assessment_year = Column(String, nullable=True)

    # user_payment / admin
    source = Column(String, nullable=False, index=True)

    # queued / generating / generated / failed
    status = Column(
        String,
        nullable=False,
        default="queued",
        index=True,
    )

    file_name = Column(String, nullable=True)
    error_message = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    generated_at = Column(DateTime, nullable=True)

    download_count = Column(Integer, default=0)
    first_downloaded_at = Column(DateTime, nullable=True)
    last_downloaded_at = Column(DateTime, nullable=True)

    user = relationship(
        "User",
        back_populates="form16_generations",
    )

    payment = relationship(
        "Payment",
        back_populates="form16_generations",
    )

    email_deliveries = relationship(
        "EmailDelivery",
        back_populates="generation",
    )


class EmailDelivery(Base):
    """
    Email queue/audit table for both transactional and reminder emails.

    Typical email_type values:
    abandoned_reminder / payment_confirmed_form16 / admin_generated_form16
    """

    __tablename__ = "email_deliveries"

    id = Column(String, primary_key=True)

    user_id = Column(
        String,
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )

    visitor_session_id = Column(
        String,
        ForeignKey("visitor_sessions.id"),
        nullable=True,
        index=True,
    )

    payment_id = Column(
        String,
        ForeignKey("payments.id"),
        nullable=True,
        index=True,
    )

    generation_id = Column(
        String,
        ForeignKey("form16_generations.id"),
        nullable=True,
        index=True,
    )

    recipient_email = Column(String, nullable=False, index=True)
    email_type = Column(String, nullable=False, index=True)
    subject = Column(String, nullable=True)

    # queued / sending / sent / failed / cancelled
    status = Column(
        String,
        nullable=False,
        default="queued",
        index=True,
    )

    scheduled_for = Column(DateTime, nullable=True, index=True)
    attempt_count = Column(Integer, default=0)

    provider_message_id = Column(String, nullable=True)
    error_message = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    sent_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)

    user = relationship(
        "User",
        back_populates="email_deliveries",
    )

    visitor_session = relationship(
        "VisitorSession",
        back_populates="email_deliveries",
    )

    payment = relationship(
        "Payment",
        back_populates="email_deliveries",
    )

    generation = relationship(
        "Form16Generation",
        back_populates="email_deliveries",
    )


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

    # TEST 2: User contact storage + exact month/year ledger identity.
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

    # TEST 3: Visitor journey + funnel event.
    visitor_id = str(uuid.uuid4())
    visitor_session_id = str(uuid.uuid4())

    journey = VisitorSession(
        id=visitor_session_id,
        visitor_id=visitor_id,
        user_id=user_id,
        email="test@test.com",
        mobile="9999999999",
        current_page="review",
        current_step="contact_details",
        last_completed_step="salary_upload",
        progress_percent=40,
        application_status="in_progress",
        payment_status="not_started",
        email_contact_consent=True,
        reminder_status="pending",
        resume_token=uuid.uuid4().hex,
        form_snapshot_json={
            "name": "Test Employee",
            "office_school_name": "Test School",
        },
    )

    session.add(journey)
    session.flush()

    session.add(
        VisitorEvent(
            id=str(uuid.uuid4()),
            session_id=visitor_session_id,
            visitor_id=visitor_id,
            user_id=user_id,
            event_type="step_completed",
            page_name="review",
            step_name="contact_details",
        )
    )

    session.commit()

    print(
        "✓ Visitor journey:",
        journey.current_page,
        "| Progress:",
        journey.progress_percent,
    )

    # TEST 4: Payment-backed Form 16 generation + email audit record.
    payment_id = str(uuid.uuid4())

    payment = Payment(
        id=payment_id,
        user_id=user_id,
        amount=150.0,
        upi_txn_utr="TEST-UTR-001",
        status="approved",
        approved_at=datetime.utcnow(),
    )

    session.add(payment)
    session.flush()

    generation_id = str(uuid.uuid4())

    generation = Form16Generation(
        id=generation_id,
        user_id=user_id,
        payment_id=payment_id,
        financial_year="2025-26",
        assessment_year="2026-27",
        source="user_payment",
        status="generated",
        file_name="Form_16_Test_Employee_FY_2025-26.pdf",
        generated_at=datetime.utcnow(),
    )

    session.add(generation)
    session.flush()

    email_delivery = EmailDelivery(
        id=str(uuid.uuid4()),
        user_id=user_id,
        visitor_session_id=visitor_session_id,
        payment_id=payment_id,
        generation_id=generation_id,
        recipient_email="test@test.com",
        email_type="payment_confirmed_form16",
        subject="Payment Confirmed – Your Form 16 for FY 2025-26",
        status="queued",
    )

    session.add(email_delivery)
    session.commit()

    print(
        "✓ Form 16 generation:",
        generation.source,
        "| Email:",
        email_delivery.status,
    )

    session.close()
