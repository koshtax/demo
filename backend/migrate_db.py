import sqlite3
from pathlib import Path


DB_PATH = Path(__file__).resolve().parent / "form16_database.db"


def table_exists(cursor, table_name):
    cursor.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (table_name,),
    )
    return cursor.fetchone() is not None


def get_columns(cursor, table_name):
    if not table_exists(cursor, table_name):
        return set()

    cursor.execute(f'PRAGMA table_info("{table_name}")')
    return {row[1] for row in cursor.fetchall()}


def add_column_if_missing(
    cursor,
    table_name,
    column_name,
    column_definition,
):
    if not table_exists(cursor, table_name):
        print(f"[SKIP] Table {table_name} does not exist yet.")
        return

    columns = get_columns(cursor, table_name)

    if column_name in columns:
        print(f"[OK] {table_name}.{column_name} already exists")
        return

    cursor.execute(
        f'ALTER TABLE "{table_name}" '
        f'ADD COLUMN "{column_name}" '
        f"{column_definition}"
    )

    print(f"[ADDED] {table_name}.{column_name}")


def create_index_safely(
    cursor,
    index_name,
    table_name,
    columns,
    unique=False,
):
    if not table_exists(cursor, table_name):
        return

    existing_columns = get_columns(cursor, table_name)

    if not set(columns).issubset(existing_columns):
        return

    unique_sql = "UNIQUE " if unique else ""
    columns_sql = ", ".join(f'"{column}"' for column in columns)

    try:
        cursor.execute(
            f'CREATE {unique_sql}INDEX IF NOT EXISTS "{index_name}" '
            f'ON "{table_name}" ({columns_sql})'
        )
    except sqlite3.IntegrityError:
        print(
            f"[WARNING] Could not create unique index {index_name}; "
            "duplicate old data was preserved."
        )


def create_missing_tables(cursor):
    # --------------------------------------------------
    # Existing core tables
    # --------------------------------------------------
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id VARCHAR PRIMARY KEY,
            email VARCHAR,
            mobile VARCHAR,
            created_at DATETIME
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_settings (
            id INTEGER PRIMARY KEY,
            fee_amount FLOAT DEFAULT 150.0,
            upi_id VARCHAR,
            telegram_bot_token VARCHAR,
            telegram_chat_id VARCHAR,
            financial_year VARCHAR,
            tax_slabs_json JSON
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS employers_by_tan (
            tan VARCHAR PRIMARY KEY,
            officer_name VARCHAR,
            officer_father_name VARCHAR,
            employer_name VARCHAR,
            employer_address VARCHAR,
            designation VARCHAR,
            pan VARCHAR,
            updated_at DATETIME
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS employee_details (
            id VARCHAR PRIMARY KEY,
            user_id VARCHAR NOT NULL UNIQUE,
            tan_id VARCHAR,
            name VARCHAR,
            pan VARCHAR,
            office_school_name VARCHAR,
            FOREIGN KEY(user_id)
                REFERENCES users(id),
            FOREIGN KEY(tan_id)
                REFERENCES employers_by_tan(tan)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS monthly_ledger (
            id VARCHAR PRIMARY KEY,
            user_id VARCHAR NOT NULL,
            month INTEGER NOT NULL,
            year INTEGER NOT NULL,
            month_year VARCHAR,
            financial_year VARCHAR,
            basic_pay FLOAT DEFAULT 0.0,
            da FLOAT DEFAULT 0.0,
            hra FLOAT DEFAULT 0.0,
            gross_salary FLOAT DEFAULT 0.0,
            line_items_json JSON,
            deductions_json JSON,
            is_auto_generated BOOLEAN DEFAULT 0,
            source VARCHAR DEFAULT 'extracted',
            note TEXT,
            flags JSON,
            created_at DATETIME,
            updated_at DATETIME,
            FOREIGN KEY(user_id)
                REFERENCES users(id)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id VARCHAR PRIMARY KEY,
            user_id VARCHAR NOT NULL,
            amount FLOAT,
            upi_txn_utr VARCHAR,
            status VARCHAR DEFAULT 'pending',
            approved_at DATETIME,
            created_at DATETIME,
            FOREIGN KEY(user_id)
                REFERENCES users(id)
        )
        """
    )

    # --------------------------------------------------
    # Visitor/session journey tracking
    # --------------------------------------------------
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS visitor_sessions (
            id VARCHAR PRIMARY KEY,
            visitor_id VARCHAR NOT NULL,
            user_id VARCHAR,
            email VARCHAR,
            mobile VARCHAR,
            current_page VARCHAR,
            current_step VARCHAR,
            last_completed_step VARCHAR,
            progress_percent INTEGER DEFAULT 0,
            application_status VARCHAR NOT NULL DEFAULT 'in_progress',
            payment_status VARCHAR NOT NULL DEFAULT 'not_started',
            form_snapshot_json JSON,
            resume_token VARCHAR,
            email_contact_consent BOOLEAN DEFAULT 0,
            reminder_status VARCHAR NOT NULL DEFAULT 'not_eligible',
            reminder_due_at DATETIME,
            reminder_sent_at DATETIME,
            started_at DATETIME,
            last_seen_at DATETIME,
            completed_at DATETIME,
            abandoned_at DATETIME,
            FOREIGN KEY(user_id)
                REFERENCES users(id)
        )
        """
    )

    # --------------------------------------------------
    # Funnel/event analytics
    # --------------------------------------------------
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS visitor_events (
            id VARCHAR PRIMARY KEY,
            session_id VARCHAR NOT NULL,
            visitor_id VARCHAR NOT NULL,
            user_id VARCHAR,
            event_type VARCHAR NOT NULL,
            page_name VARCHAR,
            step_name VARCHAR,
            event_data_json JSON,
            created_at DATETIME,
            FOREIGN KEY(session_id)
                REFERENCES visitor_sessions(id),
            FOREIGN KEY(user_id)
                REFERENCES users(id)
        )
        """
    )

    # --------------------------------------------------
    # Permanent Form 16 generation audit
    # --------------------------------------------------
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS form16_generations (
            id VARCHAR PRIMARY KEY,
            user_id VARCHAR NOT NULL,
            payment_id VARCHAR,
            financial_year VARCHAR NOT NULL,
            assessment_year VARCHAR,
            source VARCHAR NOT NULL,
            status VARCHAR NOT NULL DEFAULT 'queued',
            file_name VARCHAR,
            error_message VARCHAR,
            created_at DATETIME,
            generated_at DATETIME,
            download_count INTEGER DEFAULT 0,
            first_downloaded_at DATETIME,
            last_downloaded_at DATETIME,
            FOREIGN KEY(user_id)
                REFERENCES users(id),
            FOREIGN KEY(payment_id)
                REFERENCES payments(id)
        )
        """
    )

    # --------------------------------------------------
    # Email queue / delivery audit
    # --------------------------------------------------
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS email_deliveries (
            id VARCHAR PRIMARY KEY,
            user_id VARCHAR,
            visitor_session_id VARCHAR,
            payment_id VARCHAR,
            generation_id VARCHAR,
            recipient_email VARCHAR NOT NULL,
            email_type VARCHAR NOT NULL,
            subject VARCHAR,
            status VARCHAR NOT NULL DEFAULT 'queued',
            scheduled_for DATETIME,
            attempt_count INTEGER DEFAULT 0,
            provider_message_id VARCHAR,
            error_message VARCHAR,
            created_at DATETIME,
            updated_at DATETIME,
            sent_at DATETIME,
            cancelled_at DATETIME,
            FOREIGN KEY(user_id)
                REFERENCES users(id),
            FOREIGN KEY(visitor_session_id)
                REFERENCES visitor_sessions(id),
            FOREIGN KEY(payment_id)
                REFERENCES payments(id),
            FOREIGN KEY(generation_id)
                REFERENCES form16_generations(id)
        )
        """
    )

    print("[OK] Required tables checked/created.")


def migrate_existing_tables(cursor):
    # --------------------------------------------------
    # users
    # --------------------------------------------------
    for column_name, definition in {
        "email": "VARCHAR",
        "mobile": "VARCHAR",
        "created_at": "DATETIME",
    }.items():
        add_column_if_missing(
            cursor,
            "users",
            column_name,
            definition,
        )

    # --------------------------------------------------
    # admin_settings
    # --------------------------------------------------
    for column_name, definition in {
        "fee_amount": "FLOAT DEFAULT 150.0",
        "upi_id": "VARCHAR",
        "telegram_bot_token": "VARCHAR",
        "telegram_chat_id": "VARCHAR",
        "financial_year": "VARCHAR",
        "tax_slabs_json": "JSON",
    }.items():
        add_column_if_missing(
            cursor,
            "admin_settings",
            column_name,
            definition,
        )

    # --------------------------------------------------
    # employers_by_tan
    # --------------------------------------------------
    for column_name, definition in {
        "officer_name": "VARCHAR",
        "officer_father_name": "VARCHAR",
        "employer_name": "VARCHAR",
        "employer_address": "VARCHAR",
        "designation": "VARCHAR",
        "pan": "VARCHAR",
        "updated_at": "DATETIME",
    }.items():
        add_column_if_missing(
            cursor,
            "employers_by_tan",
            column_name,
            definition,
        )

    # --------------------------------------------------
    # employee_details
    # --------------------------------------------------
    for column_name, definition in {
        "user_id": "VARCHAR",
        "tan_id": "VARCHAR",
        "name": "VARCHAR",
        "pan": "VARCHAR",
        "office_school_name": "VARCHAR",
    }.items():
        add_column_if_missing(
            cursor,
            "employee_details",
            column_name,
            definition,
        )

    # --------------------------------------------------
    # monthly_ledger
    # --------------------------------------------------
    for column_name, definition in {
        "user_id": "VARCHAR",
        "month": "INTEGER",
        "year": "INTEGER",
        "month_year": "VARCHAR",
        "financial_year": "VARCHAR",
        "basic_pay": "FLOAT DEFAULT 0.0",
        "da": "FLOAT DEFAULT 0.0",
        "hra": "FLOAT DEFAULT 0.0",
        "gross_salary": "FLOAT DEFAULT 0.0",
        "line_items_json": "JSON",
        "deductions_json": "JSON",
        "is_auto_generated": "BOOLEAN DEFAULT 0",
        "source": "VARCHAR DEFAULT 'extracted'",
        "note": "TEXT",
        "flags": "JSON",
        "created_at": "DATETIME",
        "updated_at": "DATETIME",
    }.items():
        add_column_if_missing(
            cursor,
            "monthly_ledger",
            column_name,
            definition,
        )

    # --------------------------------------------------
    # payments
    # --------------------------------------------------
    for column_name, definition in {
        "user_id": "VARCHAR",
        "amount": "FLOAT",
        "upi_txn_utr": "VARCHAR",
        "status": "VARCHAR DEFAULT 'pending'",
        "approved_at": "DATETIME",
        "created_at": "DATETIME",
    }.items():
        add_column_if_missing(
            cursor,
            "payments",
            column_name,
            definition,
        )

    # --------------------------------------------------
    # visitor_sessions
    # Definitions here are migration-safe if a partial table
    # exists from an interrupted deployment.
    # --------------------------------------------------
    for column_name, definition in {
        "visitor_id": "VARCHAR",
        "user_id": "VARCHAR",
        "email": "VARCHAR",
        "mobile": "VARCHAR",
        "current_page": "VARCHAR",
        "current_step": "VARCHAR",
        "last_completed_step": "VARCHAR",
        "progress_percent": "INTEGER DEFAULT 0",
        "application_status": "VARCHAR DEFAULT 'in_progress'",
        "payment_status": "VARCHAR DEFAULT 'not_started'",
        "form_snapshot_json": "JSON",
        "resume_token": "VARCHAR",
        "email_contact_consent": "BOOLEAN DEFAULT 0",
        "reminder_status": "VARCHAR DEFAULT 'not_eligible'",
        "reminder_due_at": "DATETIME",
        "reminder_sent_at": "DATETIME",
        "started_at": "DATETIME",
        "last_seen_at": "DATETIME",
        "completed_at": "DATETIME",
        "abandoned_at": "DATETIME",
    }.items():
        add_column_if_missing(
            cursor,
            "visitor_sessions",
            column_name,
            definition,
        )

    # --------------------------------------------------
    # visitor_events
    # --------------------------------------------------
    for column_name, definition in {
        "session_id": "VARCHAR",
        "visitor_id": "VARCHAR",
        "user_id": "VARCHAR",
        "event_type": "VARCHAR",
        "page_name": "VARCHAR",
        "step_name": "VARCHAR",
        "event_data_json": "JSON",
        "created_at": "DATETIME",
    }.items():
        add_column_if_missing(
            cursor,
            "visitor_events",
            column_name,
            definition,
        )

    # --------------------------------------------------
    # form16_generations
    # --------------------------------------------------
    for column_name, definition in {
        "user_id": "VARCHAR",
        "payment_id": "VARCHAR",
        "financial_year": "VARCHAR",
        "assessment_year": "VARCHAR",
        "source": "VARCHAR",
        "status": "VARCHAR DEFAULT 'queued'",
        "file_name": "VARCHAR",
        "error_message": "VARCHAR",
        "created_at": "DATETIME",
        "generated_at": "DATETIME",
        "download_count": "INTEGER DEFAULT 0",
        "first_downloaded_at": "DATETIME",
        "last_downloaded_at": "DATETIME",
    }.items():
        add_column_if_missing(
            cursor,
            "form16_generations",
            column_name,
            definition,
        )

    # --------------------------------------------------
    # email_deliveries
    # --------------------------------------------------
    for column_name, definition in {
        "user_id": "VARCHAR",
        "visitor_session_id": "VARCHAR",
        "payment_id": "VARCHAR",
        "generation_id": "VARCHAR",
        "recipient_email": "VARCHAR",
        "email_type": "VARCHAR",
        "subject": "VARCHAR",
        "status": "VARCHAR DEFAULT 'queued'",
        "scheduled_for": "DATETIME",
        "attempt_count": "INTEGER DEFAULT 0",
        "provider_message_id": "VARCHAR",
        "error_message": "VARCHAR",
        "created_at": "DATETIME",
        "updated_at": "DATETIME",
        "sent_at": "DATETIME",
        "cancelled_at": "DATETIME",
    }.items():
        add_column_if_missing(
            cursor,
            "email_deliveries",
            column_name,
            definition,
        )


def backfill_existing_data(cursor):
    # --------------------------------------------------
    # Exact month identity: YYYY-MM
    # --------------------------------------------------
    if table_exists(cursor, "monthly_ledger"):
        columns = get_columns(cursor, "monthly_ledger")

        if {
            "month",
            "year",
            "month_year",
        }.issubset(columns):
            cursor.execute(
                """
                UPDATE monthly_ledger
                SET month_year =
                    printf(
                        '%04d-%02d',
                        year,
                        month
                    )
                WHERE
                    year IS NOT NULL
                    AND month IS NOT NULL
                    AND (
                        month_year IS NULL
                        OR TRIM(month_year) = ''
                    )
                """
            )

        # March -> February cycle:
        # Mar-Dec: year -> year+1
        # Jan-Feb: year-1 -> year
        if {
            "month",
            "year",
            "financial_year",
        }.issubset(columns):
            cursor.execute(
                """
                UPDATE monthly_ledger
                SET financial_year =
                    CASE
                        WHEN month >= 3 THEN
                            printf(
                                '%04d-%02d',
                                year,
                                (year + 1) % 100
                            )
                        ELSE
                            printf(
                                '%04d-%02d',
                                year - 1,
                                year % 100
                            )
                    END
                WHERE
                    year IS NOT NULL
                    AND month IS NOT NULL
                    AND (
                        financial_year IS NULL
                        OR TRIM(financial_year) = ''
                    )
                """
            )

        if "source" in columns:
            cursor.execute(
                """
                UPDATE monthly_ledger
                SET source = 'extracted'
                WHERE source IS NULL
                   OR TRIM(source) = ''
                """
            )

        if "is_auto_generated" in columns:
            cursor.execute(
                """
                UPDATE monthly_ledger
                SET is_auto_generated = 0
                WHERE is_auto_generated IS NULL
                """
            )

    # --------------------------------------------------
    # Safe defaults for newly introduced tracking tables
    # if a previous partial deployment created null rows.
    # --------------------------------------------------
    if table_exists(cursor, "visitor_sessions"):
        columns = get_columns(cursor, "visitor_sessions")

        if "progress_percent" in columns:
            cursor.execute(
                """
                UPDATE visitor_sessions
                SET progress_percent = 0
                WHERE progress_percent IS NULL
                """
            )

        if "application_status" in columns:
            cursor.execute(
                """
                UPDATE visitor_sessions
                SET application_status = 'in_progress'
                WHERE application_status IS NULL
                   OR TRIM(application_status) = ''
                """
            )

        if "payment_status" in columns:
            cursor.execute(
                """
                UPDATE visitor_sessions
                SET payment_status = 'not_started'
                WHERE payment_status IS NULL
                   OR TRIM(payment_status) = ''
                """
            )

        if "email_contact_consent" in columns:
            cursor.execute(
                """
                UPDATE visitor_sessions
                SET email_contact_consent = 0
                WHERE email_contact_consent IS NULL
                """
            )

        if "reminder_status" in columns:
            cursor.execute(
                """
                UPDATE visitor_sessions
                SET reminder_status = 'not_eligible'
                WHERE reminder_status IS NULL
                   OR TRIM(reminder_status) = ''
                """
            )

    if table_exists(cursor, "form16_generations"):
        columns = get_columns(cursor, "form16_generations")

        if "status" in columns:
            cursor.execute(
                """
                UPDATE form16_generations
                SET status = 'queued'
                WHERE status IS NULL
                   OR TRIM(status) = ''
                """
            )

        if "download_count" in columns:
            cursor.execute(
                """
                UPDATE form16_generations
                SET download_count = 0
                WHERE download_count IS NULL
                """
            )

    if table_exists(cursor, "email_deliveries"):
        columns = get_columns(cursor, "email_deliveries")

        if "status" in columns:
            cursor.execute(
                """
                UPDATE email_deliveries
                SET status = 'queued'
                WHERE status IS NULL
                   OR TRIM(status) = ''
                """
            )

        if "attempt_count" in columns:
            cursor.execute(
                """
                UPDATE email_deliveries
                SET attempt_count = 0
                WHERE attempt_count IS NULL
                """
            )


def create_indexes(cursor):
    # --------------------------------------------------
    # Existing core indexes
    # --------------------------------------------------
    create_index_safely(
        cursor,
        "ix_users_email",
        "users",
        ["email"],
        unique=True,
    )

    create_index_safely(
        cursor,
        "uq_users_mobile",
        "users",
        ["mobile"],
        unique=True,
    )

    create_index_safely(
        cursor,
        "uq_employee_details_user_id",
        "employee_details",
        ["user_id"],
        unique=True,
    )

    create_index_safely(
        cursor,
        "ix_monthly_ledger_user_id",
        "monthly_ledger",
        ["user_id"],
    )

    create_index_safely(
        cursor,
        "ix_monthly_ledger_financial_year",
        "monthly_ledger",
        ["financial_year"],
    )

    create_index_safely(
        cursor,
        "uq_monthly_ledger_user_month_year",
        "monthly_ledger",
        ["user_id", "month_year"],
        unique=True,
    )

    create_index_safely(
        cursor,
        "ix_payments_user_id",
        "payments",
        ["user_id"],
    )

    create_index_safely(
        cursor,
        "ix_payments_upi_txn_utr",
        "payments",
        ["upi_txn_utr"],
        unique=True,
    )

    # --------------------------------------------------
    # visitor_sessions indexes
    # --------------------------------------------------
    for index_name, columns in {
        "ix_visitor_sessions_visitor_id": ["visitor_id"],
        "ix_visitor_sessions_user_id": ["user_id"],
        "ix_visitor_sessions_email": ["email"],
        "ix_visitor_sessions_current_page": ["current_page"],
        "ix_visitor_sessions_application_status": ["application_status"],
        "ix_visitor_sessions_payment_status": ["payment_status"],
        "ix_visitor_sessions_reminder_status": ["reminder_status"],
        "ix_visitor_sessions_reminder_due_at": ["reminder_due_at"],
        "ix_visitor_sessions_started_at": ["started_at"],
        "ix_visitor_sessions_last_seen_at": ["last_seen_at"],
    }.items():
        create_index_safely(
            cursor,
            index_name,
            "visitor_sessions",
            columns,
        )

    create_index_safely(
        cursor,
        "ix_visitor_sessions_resume_token",
        "visitor_sessions",
        ["resume_token"],
        unique=True,
    )

    # --------------------------------------------------
    # visitor_events indexes
    # --------------------------------------------------
    for index_name, columns in {
        "ix_visitor_events_session_id": ["session_id"],
        "ix_visitor_events_visitor_id": ["visitor_id"],
        "ix_visitor_events_user_id": ["user_id"],
        "ix_visitor_events_event_type": ["event_type"],
        "ix_visitor_events_page_name": ["page_name"],
        "ix_visitor_events_created_at": ["created_at"],
    }.items():
        create_index_safely(
            cursor,
            index_name,
            "visitor_events",
            columns,
        )

    # --------------------------------------------------
    # form16_generations indexes
    # --------------------------------------------------
    for index_name, columns in {
        "ix_form16_generations_user_id": ["user_id"],
        "ix_form16_generations_payment_id": ["payment_id"],
        "ix_form16_generations_financial_year": ["financial_year"],
        "ix_form16_generations_source": ["source"],
        "ix_form16_generations_status": ["status"],
        "ix_form16_generations_created_at": ["created_at"],
    }.items():
        create_index_safely(
            cursor,
            index_name,
            "form16_generations",
            columns,
        )

    # --------------------------------------------------
    # email_deliveries indexes
    # --------------------------------------------------
    for index_name, columns in {
        "ix_email_deliveries_user_id": ["user_id"],
        "ix_email_deliveries_visitor_session_id": ["visitor_session_id"],
        "ix_email_deliveries_payment_id": ["payment_id"],
        "ix_email_deliveries_generation_id": ["generation_id"],
        "ix_email_deliveries_recipient_email": ["recipient_email"],
        "ix_email_deliveries_email_type": ["email_type"],
        "ix_email_deliveries_status": ["status"],
        "ix_email_deliveries_scheduled_for": ["scheduled_for"],
        "ix_email_deliveries_created_at": ["created_at"],
    }.items():
        create_index_safely(
            cursor,
            index_name,
            "email_deliveries",
            columns,
        )


def verify_schema(cursor):
    required_tables = {
        "users",
        "admin_settings",
        "employers_by_tan",
        "employee_details",
        "monthly_ledger",
        "payments",
        "visitor_sessions",
        "visitor_events",
        "form16_generations",
        "email_deliveries",
    }

    missing_tables = [
        table_name
        for table_name in required_tables
        if not table_exists(cursor, table_name)
    ]

    if missing_tables:
        raise RuntimeError(
            "Migration verification failed. Missing tables: "
            + ", ".join(sorted(missing_tables))
        )

    required_tracking_columns = {
        "visitor_sessions": {
            "id",
            "visitor_id",
            "user_id",
            "email",
            "mobile",
            "current_page",
            "current_step",
            "last_completed_step",
            "progress_percent",
            "application_status",
            "payment_status",
            "form_snapshot_json",
            "resume_token",
            "email_contact_consent",
            "reminder_status",
            "reminder_due_at",
            "reminder_sent_at",
            "started_at",
            "last_seen_at",
            "completed_at",
            "abandoned_at",
        },
        "visitor_events": {
            "id",
            "session_id",
            "visitor_id",
            "user_id",
            "event_type",
            "page_name",
            "step_name",
            "event_data_json",
            "created_at",
        },
        "form16_generations": {
            "id",
            "user_id",
            "payment_id",
            "financial_year",
            "assessment_year",
            "source",
            "status",
            "file_name",
            "error_message",
            "created_at",
            "generated_at",
            "download_count",
            "first_downloaded_at",
            "last_downloaded_at",
        },
        "email_deliveries": {
            "id",
            "user_id",
            "visitor_session_id",
            "payment_id",
            "generation_id",
            "recipient_email",
            "email_type",
            "subject",
            "status",
            "scheduled_for",
            "attempt_count",
            "provider_message_id",
            "error_message",
            "created_at",
            "updated_at",
            "sent_at",
            "cancelled_at",
        },
    }

    for table_name, required_columns in required_tracking_columns.items():
        actual_columns = get_columns(cursor, table_name)
        missing_columns = required_columns - actual_columns

        if missing_columns:
            raise RuntimeError(
                f"Migration verification failed for {table_name}. "
                f"Missing columns: {', '.join(sorted(missing_columns))}"
            )

    print("[OK] Schema verification passed.")


def migrate():
    print(f"[INFO] Database path: {DB_PATH}")

    connection = sqlite3.connect(DB_PATH)

    try:
        cursor = connection.cursor()

        # Enforce FK checks for this migration connection.
        cursor.execute("PRAGMA foreign_keys = ON")

        # 1. Ensure all old + new model tables exist.
        create_missing_tables(cursor)

        # 2. Upgrade older/partially-created tables safely.
        migrate_existing_tables(cursor)

        # 3. Preserve/backfill existing ledger data and safe defaults.
        backfill_existing_data(cursor)

        # 4. Restore all required indexes and uniqueness rules.
        create_indexes(cursor)

        # 5. Verify the critical schema before committing.
        verify_schema(cursor)

        connection.commit()

        print("[SUCCESS] Database migration completed.")

    except Exception:
        connection.rollback()

        print(
            "[ERROR] Database migration failed. "
            "Changes rolled back."
        )

        raise

    finally:
        connection.close()


if __name__ == "__main__":
    migrate()
