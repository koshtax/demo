"""
Monthly ledger automation.

Govt salary bill cycle here runs **March to February** (not the standard
Apr-Mar financial year) — so January is the second-last month of the cycle
and February is the last/settlement month. That's why the automation lands
exactly there:
  - January: auto-generate if latest slip is Dec/Jan. Uses June-vs-July
    basic pay to decide if an increment is due, then projects EVERY
    allowance field (DA, HRA, TA, Medical, whatever else exists) forward
    onto the new basic using whatever pattern component_classifier detects
    for that field from the employee's own history — not a hardcoded
    assumption that "DA/HRA are %, Medical is flat". A field the classifier
    can't confidently place (dynamic / insufficient_data) is carried
    forward unchanged and flagged for manual review instead of guessed.
  - February (last month of the cycle): compute the full cycle's tax
    liability (new regime), subtract TDS already deducted Mar-Jan (the
    first 11 months), deduct the remainder in Feb as final settlement.

NOTE: If June or July is a combined/multi-month bill (no clean single-month
basic pay) or missing, the increment check is SKIPPED for that cycle and
January's basic carries forward from December unchanged. Flag for review.
"""

from .pay_matrix import build_full_matrix, find_level_and_cell, next_cell
from .tax_calculator import compute_annual_tax
from .component_classifier import classify_all_fields, project_field_forward

STRUCTURAL_KEYS = {
    "id",
    "user_id",
    "month",
    "year",
    "month_year",
    "financial_year",
    "basic_pay",
    "source",
    "is_auto_generated",
    "gross_salary",
    "net_pay",
    "tds_deducted",
    "line_items",
    "line_items_json",
    "deductions",
    "deductions_json",
    "gpf",
    "gli",
    "prof_tax",
    "tds",
    "note",
    "flags",
}


def is_combined_bill(month_entry: dict) -> bool:
    if month_entry is None:
        return True

    return (
        month_entry.get("source") in {"combined_period", "arrear"}
        or month_entry.get("is_arrear") is True
    )

def parse_financial_year(financial_year: str):
    """
    Admin-selected FY ko cycle start/end year me convert karta hai.

    Example:
        "2026-27" -> (2026, 2027)

    Payroll cycle:
        March 2026 ... February 2027
    """

    if not financial_year:
        raise ValueError("Financial year is required.")

    value = str(financial_year).strip()

    try:
        start_text, end_text = value.split("-", 1)
        start_year = int(start_text)

        if len(end_text) == 2:
            end_year = (start_year // 100) * 100 + int(end_text)
        else:
            end_year = int(end_text)

    except (ValueError, TypeError):
        raise ValueError(
            f"Invalid financial year: {financial_year}. Expected format YYYY-YY."
        )

    if end_year != start_year + 1:
        raise ValueError(
            f"Invalid financial year: {financial_year}. Years must be consecutive."
        )

    return start_year, end_year

def generate_january_ledger(
    history: list,
    matrix: dict,
    financial_year: str
) -> dict:
    """
    history: chronological list of monthly ledger dicts for Mar..Dec of the
    cycle so far. Each is a flat dict: {month, year, basic_pay, source,
    <allowance fields...>}. The last entry must be December.
    Returns the auto-generated January ledger dict.
    """
    start_year, end_year = parse_financial_year(financial_year)

december = next(
    (
        m for m in reversed(history)
        if m.get("month") == 12
        and m.get("year") == start_year
        and not is_combined_bill(m)
    ),
    None,
)

june = next(
    (
        m for m in reversed(history)
        if m.get("month") == 6
        and m.get("year") == start_year
        and not is_combined_bill(m)
    ),
    None,
)

july = next(
    (
        m for m in reversed(history)
        if m.get("month") == 7
        and m.get("year") == start_year
        and not is_combined_bill(m)
    ),
    None,
)

if december is None:
    raise ValueError(
        f"Clean December salary entry not found for FY {financial_year}."
    )

    notes = []
    if is_combined_bill(june) or is_combined_bill(july):
        jan_basic = december["basic_pay"]
        notes.append("June/July unavailable or a combined-period bill; increment check skipped.")
    else:
        level, _ = find_level_and_cell(december["basic_pay"], matrix)
        if level is None:
            jan_basic = december["basic_pay"]
            notes.append("December's basic pay doesn't match any pay-matrix cell exactly; increment skipped — needs manual review.")
        elif june["basic_pay"] == july["basic_pay"]:
            jan_basic = next_cell(level, december["basic_pay"], matrix)  # no increment had landed -> apply it now
        else:
            jan_basic = december["basic_pay"]  # increment already happened somewhere this cycle -> don't double it

    classifications = classify_all_fields(history)
    jan_ledger = {
        "month": 1,
        "year": end_year,
        "basic_pay": jan_basic,
        "source": "auto_generated",
        "is_auto_generated": True,
    }

    flags = []
    for field, pattern in classifications.items():
        projected = project_field_forward(field, pattern, december, jan_basic)
        if projected is None:
            jan_ledger[field] = december.get(field)
            flags.append(f"'{field}': pattern={pattern} — carried forward unchanged, needs manual review.")
        else:
            jan_ledger[field] = projected

    if notes:
        jan_ledger["note"] = " ".join(notes)
    if flags:
        jan_ledger["flags"] = flags

    component_sum = sum(v for k, v in jan_ledger.items() if k not in STRUCTURAL_KEYS)
    jan_ledger["gross_salary"] = round(jan_basic + component_sum, 2)
    return jan_ledger


def compute_february_tds(cycle_months_mar_to_jan: list, feb_projected_gross: float) -> dict:
    """
    cycle_months_mar_to_jan: list of ledger dicts for Mar..Jan (11 months of
       actual/auto-generated figures), each optionally carrying
       'tds_deducted' (default 0 if absent).
    feb_projected_gross: February's own gross salary (12th and final month
       of the cycle — nothing projected beyond it, unlike an Apr-Mar FY
       where Feb/Mar both had to be estimated).

    Logic (as confirmed):
      1. Total gross for the cycle = sum(Mar..Jan actual) + Feb
      2. Total tax that SHOULD be cut for the full cycle = compute_annual_tax(total gross)
      3. Total TDS already cut so far (Mar..Jan) = sum of tds_deducted so far
      4. Feb deduction = total_tax_due - tds_already_cut  (floor at 0)
    """
    gross_so_far = sum(m["gross_salary"] for m in cycle_months_mar_to_jan)
    total_cycle_gross = gross_so_far + feb_projected_gross

    tax_breakdown = compute_annual_tax(total_cycle_gross)
    total_tax_due = tax_breakdown["total_tax_payable"]

    tds_already_cut = sum(m.get("tds_deducted", 0) for m in cycle_months_mar_to_jan)

    feb_deduction = max(0, total_tax_due - tds_already_cut)

    return {
        "total_cycle_gross": total_cycle_gross,
        "tax_breakdown": tax_breakdown,
        "tds_already_cut": tds_already_cut,
        "feb_tds_deduction": feb_deduction,
        "feb_net_payable": round(feb_projected_gross - feb_deduction, 2),
    }


if __name__ == "__main__":
    matrix = build_full_matrix()

    # =========================================================
    # TEST 1 — Real ground-truth data: VIVEK KUMAR PANDEY
    # Full Mar'25-Dec'25 history is available -> increment check
    # AND field classification both run for real, and we can compare
    # the auto-generated January against the ACTUAL January on the slip.
    # =========================================================
    vivek_history = [
        {"month": 3, "year": 2025, "basic_pay": 49000, "da": 25970, "hra": 4900, "medical": 500, "source": "extracted"},
        {"month": 4, "year": 2025, "basic_pay": 49000, "da": 25970, "hra": 4900, "medical": 500, "source": "extracted"},
        {"month": 5, "year": 2025, "basic_pay": 49000, "da": 26950, "hra": 4900, "medical": 500, "source": "extracted"},
        {"month": 6, "year": 2025, "basic_pay": 49000, "da": 26950, "hra": 4900, "medical": 500, "source": "extracted"},
        {"month": 7, "year": 2025, "basic_pay": 49000, "da": 26950, "hra": 4900, "medical": 500, "source": "extracted"},
        {"month": 8, "year": 2025, "basic_pay": 49000, "da": 26950, "hra": 4900, "medical": 500, "source": "extracted"},
        {"month": 9, "year": 2025, "basic_pay": 49000, "da": 26950, "hra": 4900, "medical": 500, "source": "extracted"},
        {"month": 10, "year": 2025, "basic_pay": 49000, "da": 28420, "hra": 4900, "medical": 500, "source": "extracted"},
        {"month": 11, "year": 2025, "basic_pay": 49000, "da": 28420, "hra": 4900, "medical": 500, "source": "extracted"},
        {"month": 12, "year": 2025, "basic_pay": 49000, "da": 28420, "hra": 4900, "medical": 500, "source": "extracted"},
    ]

    level, cell = find_level_and_cell(49000, matrix)
    print(f"December's basic 49000 -> matrix Level {level}, Cell {cell}")

    jan_auto = generate_january_ledger(vivek_history, matrix)
    print("\nAuto-generated January (Vivek):")
    print(jan_auto)

    actual_jan = {"basic_pay": 50500, "da": 29290, "hra": 5050, "medical": 500}
    print("\nComparing against ACTUAL January from the real slip:")
    for field, actual_val in actual_jan.items():
        predicted = jan_auto.get(field)
        match = "✓" if predicted == actual_val else "✗ MISMATCH"
        print(f"  {field}: predicted={predicted}, actual={actual_val}  {match}")

    # =========================================================
    # TEST 2 — JAYA KUMARI: combined Jun-Oct bill -> increment skipped
    # =========================================================
    jaya_history = [
        {"month": 6, "year": 2025, "source": "combined_period"},  # Jun-Oct combined, no clean split
        {"month": 11, "year": 2025, "basic_pay": 19900, "da": 11542, "hra": 1990, "medical": 500, "source": "extracted"},
        {"month": 12, "year": 2025, "basic_pay": 19900, "da": 11542, "hra": 1990, "medical": 500, "source": "extracted"},
    ]
    jan_jaya = generate_january_ledger(jaya_history, matrix)
    print("\n\nAuto-generated January (Jaya, combined-bill case):")
    print(jan_jaya)

    # =========================================================
    # Feb TDS simulation (using Vivek's real full-cycle gross figures)
    # =========================================================
    cycle_months = [
        {"gross_salary": 80370, "tds_deducted": 0},  # Mar
        {"gross_salary": 80370, "tds_deducted": 0},  # Apr
        {"gross_salary": 81350, "tds_deducted": 0},  # May
        {"gross_salary": 81350, "tds_deducted": 0},  # Jun
        {"gross_salary": 81350, "tds_deducted": 0},  # Jul
        {"gross_salary": 81350, "tds_deducted": 0},  # Aug
        {"gross_salary": 81350, "tds_deducted": 0},  # Sep
        {"gross_salary": 82820, "tds_deducted": 0},  # Oct
        {"gross_salary": 82820, "tds_deducted": 0},  # Nov
        {"gross_salary": 82820, "tds_deducted": 0},  # Dec
        {"gross_salary": jan_auto["gross_salary"], "tds_deducted": 0},  # Jan (auto)
    ]
    feb_result = compute_february_tds(cycle_months, jan_auto["gross_salary"])
    print("\nFebruary TDS computation (Vivek):")
    print(feb_result)
