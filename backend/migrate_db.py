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
        print(
            f"[SKIP] Table {table_name} does not exist yet."
        )
        return

    columns = get_columns(cursor, table_name)

    if column_name in columns:
        print(
            f"[OK] {table_name}.{column_name} "
            "already exists"
        )
        return

    cursor.execute(
        f'ALTER TABLE "{table_name}" '
        f'ADD COLUMN "{column_name}" '
        f"{column_definition}"
    )

    print(
        f"[ADDED] {table_name}.{column_name}"
    )


def create_missing_tables(cursor):
    # --------------------------------------------------
    # users
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
        CREATE UNIQUE INDEX IF NOT EXISTS
        ix_users_email
        ON users(email)
        """
    )

    # --------------------------------------------------
    # admin_settings
    # --------------------------------------------------
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

    # --------------------------------------------------
    # employers_by_tan
    # --------------------------------------------------
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
        CREATE INDEX IF NOT EXISTS
        ix_monthly_ledger_financial_year
        ON monthly_ledger(financial_year)
    """
    )
    # --------------------------------------------------
    # employee_details
    # --------------------------------------------------
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

    # --------------------------------------------------
    # monthly_ledger
    # --------------------------------------------------
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
        CREATE INDEX IF NOT EXISTS
        ix_monthly_ledger_financial_year
        ON monthly_ledger(financial_year)
        """
    )

    # --------------------------------------------------
    # payments
    # --------------------------------------------------
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

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS
        ix_payments_user_id
        ON payments(user_id)
        """
    )

    print("[OK] Required tables checked/created.")


def migrate_existing_tables(cursor):
    # --------------------------------------------------
    # users
    # --------------------------------------------------
    add_column_if_missing(
        cursor,
        "users",
        "email",
        "VARCHAR",
    )

    add_column_if_missing(
        cursor,
        "users",
        "mobile",
        "VARCHAR",
    )

    add_column_if_missing(
        cursor,
        "users",
        "created_at",
        "DATETIME",
    )

    # --------------------------------------------------
    # admin_settings
    # --------------------------------------------------
    add_column_if_missing(
        cursor,
        "admin_settings",
        "fee_amount",
        "FLOAT DEFAULT 150.0",
    )

    add_column_if_missing(
        cursor,
        "admin_settings",
        "upi_id",
        "VARCHAR",
    )

    add_column_if_missing(
        cursor,
        "admin_settings",
        "telegram_bot_token",
        "VARCHAR",
    )

    add_column_if_missing(
        cursor,
        "admin_settings",
        "telegram_chat_id",
        "VARCHAR",
    )

    add_column_if_missing(
        cursor,
        "admin_settings",
        "financial_year",
        "VARCHAR",
    )

    add_column_if_missing(
        cursor,
        "admin_settings",
        "tax_slabs_json",
        "JSON",
    )

    # --------------------------------------------------
    # employers_by_tan
    # --------------------------------------------------
    add_column_if_missing(
        cursor,
        "employers_by_tan",
        "officer_name",
        "VARCHAR",
    )

    add_column_if_missing(
        cursor,
        "employers_by_tan",
        "officer_father_name",
        "VARCHAR",
    )

    add_column_if_missing(
        cursor,
        "employers_by_tan",
        "employer_name",
        "VARCHAR",
    )

    add_column_if_missing(
        cursor,
        "employers_by_tan",
        "employer_address",
        "VARCHAR",
    )

    add_column_if_missing(
        cursor,
        "employers_by_tan",
        "designation",
        "VARCHAR",
    )

    add_column_if_missing(
        cursor,
        "employers_by_tan",
        "pan",
        "VARCHAR",
    )

    add_column_if_missing(
        cursor,
        "employers_by_tan",
        "updated_at",
        "DATETIME",
    )

    # --------------------------------------------------
    # employee_details
    # --------------------------------------------------
    add_column_if_missing(
        cursor,
        "employee_details",
        "user_id",
        "VARCHAR",
    )

    add_column_if_missing(
        cursor,
        "employee_details",
        "tan_id",
        "VARCHAR",
    )

    add_column_if_missing(
        cursor,
        "employee_details",
        "name",
        "VARCHAR",
    )

    add_column_if_missing(
        cursor,
        "employee_details",
        "pan",
        "VARCHAR",
    )

    add_column_if_missing(
        cursor,
        "employee_details",
        "office_school_name",
        "VARCHAR",
    )

    # --------------------------------------------------
    # monthly_ledger
    # --------------------------------------------------
    add_column_if_missing(
        cursor,
        "monthly_ledger",
        "user_id",
        "VARCHAR",
    )

    add_column_if_missing(
        cursor,
        "monthly_ledger",
        "month",
        "INTEGER",
    )

    add_column_if_missing(
        cursor,
        "monthly_ledger",
        "year",
        "INTEGER",
    )

    add_column_if_missing(
        cursor,
        "monthly_ledger",
        "month_year",
        "VARCHAR",
    )

    add_column_if_missing(
        cursor,
        "monthly_ledger",
        "financial_year",
        "VARCHAR",
    )

    add_column_if_missing(
        cursor,
        "monthly_ledger",
        "basic_pay",
        "FLOAT DEFAULT 0.0",
    )

    add_column_if_missing(
        cursor,
        "monthly_ledger",
        "da",
        "FLOAT DEFAULT 0.0",
    )

    add_column_if_missing(
        cursor,
        "monthly_ledger",
        "hra",
        "FLOAT DEFAULT 0.0",
    )

    add_column_if_missing(
        cursor,
        "monthly_ledger",
        "gross_salary",
        "FLOAT DEFAULT 0.0",
    )

    add_column_if_missing(
        cursor,
        "monthly_ledger",
        "line_items_json",
        "JSON",
    )

    add_column_if_missing(
        cursor,
        "monthly_ledger",
        "deductions_json",
        "JSON",
    )

    add_column_if_missing(
        cursor,
        "monthly_ledger",
        "is_auto_generated",
        "BOOLEAN DEFAULT 0",
    )

    add_column_if_missing(
        cursor,
        "monthly_ledger",
        "source",
        "VARCHAR DEFAULT 'extracted'",
    )

    add_column_if_missing(
        cursor,
        "monthly_ledger",
        "note",
        "TEXT",
    )

    add_column_if_missing(
        cursor,
        "monthly_ledger",
        "flags",
        "JSON",
    )

    add_column_if_missing(
        cursor,
        "monthly_ledger",
        "created_at",
        "DATETIME",
    )

    add_column_if_missing(
        cursor,
        "monthly_ledger",
        "updated_at",
        "DATETIME",
    )

    # --------------------------------------------------
    # payments
    # --------------------------------------------------
    add_column_if_missing(
        cursor,
        "payments",
        "user_id",
        "VARCHAR",
    )

    add_column_if_missing(
        cursor,
        "payments",
        "amount",
        "FLOAT",
    )

    add_column_if_missing(
        cursor,
        "payments",
        "upi_txn_utr",
        "VARCHAR",
    )

    add_column_if_missing(
        cursor,
        "payments",
        "status",
        "VARCHAR DEFAULT 'pending'",
    )

    add_column_if_missing(
        cursor,
        "payments",
        "approved_at",
        "DATETIME",
    )

    add_column_if_missing(
        cursor,
        "payments",
        "created_at",
        "DATETIME",
    )


def backfill_ledger(cursor):
    if not table_exists(cursor, "monthly_ledger"):
        return

    columns = get_columns(
        cursor,
        "monthly_ledger",
    )

    # --------------------------------------------------
    # Exact month identity: YYYY-MM
    # --------------------------------------------------
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

    # --------------------------------------------------
    # March -> February financial year
    #
    # Mar-Dec: year -> year+1
    # Jan-Feb: year-1 -> year
    # --------------------------------------------------
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

    # Fill safe defaults for migrated rows.
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


def create_indexes(cursor):
    # --------------------------------------------------
    # Users
    # --------------------------------------------------
    if table_exists(cursor, "users"):
        columns = get_columns(cursor, "users")

        if "email" in columns:
            try:
                cursor.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                    ix_users_email
                    ON users(email)
                    """
                )
            except sqlite3.IntegrityError:
                print(
                    "[WARNING] Duplicate user emails "
                    "exist; unique email index skipped."
                )

        if "mobile" in columns:
            try:
                cursor.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                    uq_users_mobile
                    ON users(mobile)
                    """
                )
            except sqlite3.IntegrityError:
                print(
                    "[WARNING] Duplicate mobile values "
                    "exist; unique mobile index skipped."
                )

    # --------------------------------------------------
    # EmployeeDetail.user_id one-to-one
    # --------------------------------------------------
    if table_exists(cursor, "employee_details"):
        columns = get_columns(
            cursor,
            "employee_details",
        )

        if "user_id" in columns:
            try:
                cursor.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                    uq_employee_details_user_id
                    ON employee_details(user_id)
                    """
                )
            except sqlite3.IntegrityError:
                print(
                    "[WARNING] Duplicate employee "
                    "user_id values exist; unique "
                    "index skipped."
                )

    # --------------------------------------------------
    # Monthly ledger
    # --------------------------------------------------
    if table_exists(cursor, "monthly_ledger"):
        columns = get_columns(
            cursor,
            "monthly_ledger",
        )

        if "user_id" in columns:
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS
                ix_monthly_ledger_user_id
                ON monthly_ledger(user_id)
                """
            )

        if "financial_year" in columns:
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS
                ix_monthly_ledger_financial_year
                ON monthly_ledger(financial_year)
                """
            )

        if {
            "user_id",
            "month_year",
        }.issubset(columns):
            try:
                cursor.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                    uq_monthly_ledger_user_month_year
                    ON monthly_ledger(
                        user_id,
                        month_year
                    )
                    """
                )
            except sqlite3.IntegrityError:
                print(
                    "[WARNING] Duplicate old ledger "
                    "rows exist. Unique month index "
                    "was skipped; data was preserved."
                )

    # --------------------------------------------------
    # Payments
    # --------------------------------------------------
    if table_exists(cursor, "payments"):
        columns = get_columns(
            cursor,
            "payments",
        )

        if "user_id" in columns:
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS
                ix_payments_user_id
                ON payments(user_id)
                """
            )

        if "upi_txn_utr" in columns:
            try:
                cursor.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                    ix_payments_upi_txn_utr
                    ON payments(upi_txn_utr)
                    """
                )
            except sqlite3.IntegrityError:
                print(
                    "[WARNING] Duplicate UTR values "
                    "exist; unique UTR index skipped."
                )


def migrate():
    print(
        f"[INFO] Database path: {DB_PATH}"
    )

    connection = sqlite3.connect(DB_PATH)

    try:
        cursor = connection.cursor()

        # Enforce FK checks for this connection.
        cursor.execute("PRAGMA foreign_keys = ON")

        # 1. Ensure every model table exists.
        create_missing_tables(cursor)

        # 2. Upgrade older existing tables.
        migrate_existing_tables(cursor)

        # 3. Back-fill newly introduced ledger identity.
        backfill_ledger(cursor)

        # 4. Restore important indexes/uniqueness.
        create_indexes(cursor)

        connection.commit()

        print(
            "[SUCCESS] Database migration completed."
        )

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
