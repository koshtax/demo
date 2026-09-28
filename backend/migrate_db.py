import sqlite3
from pathlib import Path


DB_PATH = Path(__file__).resolve().parent / "form16_database.db"


def get_columns(cursor, table_name):
    cursor.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cursor.fetchall()}


def add_column_if_missing(
    cursor,
    table_name,
    column_name,
    column_definition,
):
    columns = get_columns(cursor, table_name)

    if column_name in columns:
        print(
            f"[OK] {table_name}.{column_name} "
            "already exists"
        )
        return

    cursor.execute(
        f"ALTER TABLE {table_name} "
        f"ADD COLUMN {column_name} "
        f"{column_definition}"
    )

    print(
        f"[ADDED] {table_name}.{column_name}"
    )


def migrate():
    if not DB_PATH.exists():
        print(
            "[INFO] Database does not exist yet. "
            "SQLAlchemy will create a fresh database."
        )
        return

    print(f"[INFO] Migrating database: {DB_PATH}")

    connection = sqlite3.connect(DB_PATH)

    try:
        cursor = connection.cursor()

        # -----------------------------------------
        # admin_settings
        # -----------------------------------------
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

        # -----------------------------------------
        # employer_cache
        # -----------------------------------------
        add_column_if_missing(
            cursor,
            "employer_cache",
            "officer_father_name",
            "VARCHAR",
        )

        add_column_if_missing(
            cursor,
            "employer_cache",
            "pan",
            "VARCHAR",
        )

        add_column_if_missing(
            cursor,
            "employer_cache",
            "updated_at",
            "DATETIME",
        )

        # -----------------------------------------
        # monthly_ledger
        # -----------------------------------------
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

        # -----------------------------------------
        # payments
        # -----------------------------------------
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

        # -----------------------------------------
        # Back-fill month_year for old ledger rows
        # -----------------------------------------
        ledger_columns = get_columns(
            cursor,
            "monthly_ledger",
        )

        if {
            "month",
            "year",
            "month_year",
        }.issubset(ledger_columns):

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
                    month_year IS NULL
                    OR TRIM(month_year) = ''
                """
            )

        # -----------------------------------------
        # Back-fill financial_year
        #
        # Payroll cycle:
        # March-Dec -> same starting year
        # Jan-Feb    -> previous starting year
        # -----------------------------------------
        if {
            "month",
            "year",
            "financial_year",
        }.issubset(ledger_columns):

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
                    financial_year IS NULL
                    OR TRIM(financial_year) = ''
                """
            )

        # -----------------------------------------
        # Unique index for exact payroll month
        # -----------------------------------------
        if {
            "user_id",
            "month_year",
        }.issubset(ledger_columns):

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
                    "rows exist. Unique index was not "
                    "created. Existing data was left "
                    "untouched."
                )

        connection.commit()

        print(
            "[SUCCESS] Database migration completed."
        )

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


if __name__ == "__main__":
    migrate()
