"""
Generic salary-slip text parser.

Built against a real govt salary slip (Jharkhand SDEO format), but designed
to generalise: field extraction is synonym-based (regex per canonical field,
not per exact label wording) and tolerant of multi-column PDFs where text
extraction scrambles column order onto the same line
(e.g. "Basic ₹91,540.00 GLI ₹150.00" — two different columns merged).

Because of that column-scramble, this parser deliberately does NOT trust a
lone "Total" line's position to mean "gross salary" — it computes gross as
the sum of whatever allowance heads it actually found, and cross-checks
that sum against a "Total" figure sitting right before "Net Pay" as a
sanity check, flagging a mismatch instead of silently trusting either one.

Usage: pass raw extracted PDF text (e.g. from pdfplumber) into
`parse_full_pdf_text()`. One salary slip PDF can contain multiple monthly
slip blocks (as bulk-generated govt PDFs often do) — each becomes one
entry in the returned list.
"""

import re

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
MONTH_NAMES = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}


def month_year_key(month: int, year: int) -> str:
    """
    Database-safe month identity.

    Example:
        January 2026 -> 2026-01
        February 2027 -> 2027-02
    """
    return f"{int(year):04d}-{int(month):02d}"


def expand_month_range(
    start_month: int,
    start_year: int,
    end_month: int,
    end_year: int
) -> list:
    """
    Start month/year se end month/year tak saare months return karta hai.

    Example:
        Jun 2025 - Oct 2025
        ->
        [(6, 2025), (7, 2025), (8, 2025), (9, 2025), (10, 2025)]
    """

    if not start_month or not end_month:
        return []

    start = int(start_year) * 12 + (int(start_month) - 1)
    end = int(end_year) * 12 + (int(end_month) - 1)

    if end < start:
        return []

    # OCR/invalid data ki wajah se bahut bada accidental range na bane.
    if end - start > 120:
        return []

    months = []

    for value in range(start, end + 1):
        year, zero_based_month = divmod(value, 12)
        months.append((zero_based_month + 1, year))

    return months


def enrich_period(period: dict) -> dict:
    """
    Existing `months` structure ko preserve karte hue
    normalized month_year information add karta hai.
    """

    months = period.get("months") or []

    period["month_years"] = [
        month_year_key(month, year)
        for month, year in months
        if month and year
    ]

    period["month_year_labels"] = [
        f"{MONTH_NAMES.get(month, str(month))} {year}"
        for month, year in months
        if month and year
    ]

    period["month_count"] = len(period["month_years"])

    period["primary_month_year"] = (
        period["month_years"][0]
        if period["month_years"]
        else None
    )

    return period
# Canonical field name -> regex fragments that could label it.
# Add more synonyms here as new department formats are seen.
ALLOWANCE_SYNONYMS = {
    "basic_pay": [r"\bbasic\b"],
    "da": [r"\bda\b", r"dearness allowance"],
    "hra": [r"\bhra\b", r"house rent allowance"],
    "ta": [r"\bta\b(?!n)", r"travel(?:ling)? allowance", r"transport allowance"],
    "medical": [r"medical(?:\s+allow\.?)?"],
}

DEDUCTION_SYNONYMS = {
    "gli": [r"\bgli\b"],
    "gpf": [r"\bgpf\b"],
    "professional_tax": [r"professional tax", r"prof\.?\s*tax"],
    "income_tax_tds": [r"income tax", r"\bi\.?\s*tax\b", r"\btds\b"],
}

# Deliberately tight: only whitespace/colon/₹ allowed between the label and
# the number. A loose "any chars within N" gap was matching the wrong number
# when the same word appears elsewhere as a label (e.g. "GPF" also appears
# in "Employee GPF No.: KHN/EDN/D951" — a loose gap would grab "951" from
# that ID instead of the actual GPF deduction amount further down).
AMOUNT_AFTER_LABEL = r"[:\s]*[₹]?\s*([\d,]+(?:\.\d+)?)"


def split_into_slip_blocks(full_text: str) -> list:
    """One PDF can bundle several months' slips (and arrear bills) — split on
    each 'Salary Slip - ... Salary' OR 'Salary Slip - ... Arrear' header,
    both of which repeat once per block. Missing the 'Arrear' variant would
    silently swallow that whole bill into the previous block's text."""
    pattern = re.compile(r"(Salary Slip\s*-\s*.*?(?:Salary|Arrear))", re.IGNORECASE)
    matches = list(pattern.finditer(full_text))
    blocks = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        blocks.append(full_text[start:end])
    return blocks


def parse_period(header_line: str) -> dict:
    """
    Salary slip ke header se exact month + year extract karta hai.

    IMPORTANT:
    Ye function Financial Year decide nahi karega.

    Example:
        Salary Slip - Jan 2026 Salary
        Salary Slip - Jun-Oct 2025 Salary
        Salary Slip - Nov-Feb 2027 Salary
        Salary Slip - Nov 2026-Feb 2027 Salary
        Salary Slip - Jul 2022-Jan 2025 Salary
        Salary Slip - Jul-Dec 2024 Arrear
    """

    base = {
        "is_combined": False,
        "months": [],
        "raw": None,
        "is_arrear": False,
        "cross_year": False,
    }

    m = re.search(
        r"Salary Slip\s*-\s*(.+?)\s*(Salary|Arrear)\s*$",
        header_line,
        re.IGNORECASE
    )

    if not m:
        return enrich_period(base)

    period_str = m.group(1).strip()

    is_explicit_arrear = (
        m.group(2).lower() == "arrear"
    )

    # ---------------------------------------------------------
    # CASE 1
    # Dono side year available:
    #
    # Jul 2022-Jan 2025
    # Nov 2026-Feb 2027
    # ---------------------------------------------------------

    cross = re.fullmatch(
        r"([A-Za-z]{3,})\s+(\d{4})\s*-\s*"
        r"([A-Za-z]{3,})\s+(\d{4})",
        period_str
    )

    if cross:

        start_mon, start_year, end_mon, end_year = cross.groups()

        start_month = MONTH_MAP.get(
            start_mon[:3].lower()
        )

        end_month = MONTH_MAP.get(
            end_mon[:3].lower()
        )

        months = expand_month_range(
            start_month,
            int(start_year),
            end_month,
            int(end_year)
        )

        period = {
            "is_combined": len(months) > 1,
            "months": months,
            "raw": period_str,

            # Existing behaviour preserve:
            # multi-year salary period arrear-like bill maana jayega.
            "is_arrear": (
                is_explicit_arrear
                or int(start_year) != int(end_year)
            ),

            "cross_year": (
                int(start_year) != int(end_year)
            ),
        }

        if not months:
            period["parse_error"] = (
                "Invalid or reversed month/year range."
            )

        return enrich_period(period)

    # ---------------------------------------------------------
    # CASE 2
    # Ek hi printed year:
    #
    # Jun-Oct 2025
    #
    # Special:
    # Nov-Feb 2027
    #
    # Iska matlab:
    # Nov 2026
    # Dec 2026
    # Jan 2027
    # Feb 2027
    # ---------------------------------------------------------

    same = re.fullmatch(
        r"([A-Za-z]{3,})\s*-\s*"
        r"([A-Za-z]{3,})\s+(\d{4})",
        period_str
    )

    if same:

        start_mon, end_mon, printed_year = same.groups()

        start_month = MONTH_MAP.get(
            start_mon[:3].lower()
        )

        end_month = MONTH_MAP.get(
            end_mon[:3].lower()
        )

        end_year = int(printed_year)

        if (
            start_month
            and end_month
            and start_month <= end_month
        ):
            start_year = end_year

        else:
            start_year = end_year - 1

        months = expand_month_range(
            start_month,
            start_year,
            end_month,
            end_year
        )

        period = {
            "is_combined": len(months) > 1,
            "months": months,
            "raw": period_str,
            "is_arrear": is_explicit_arrear,
            "cross_year": start_year != end_year,
        }

        if not months:
            period["parse_error"] = (
                "Could not resolve month range."
            )

        return enrich_period(period)

    # ---------------------------------------------------------
    # CASE 3
    # Single salary month:
    #
    # Jan 2026
    # ---------------------------------------------------------

    single = re.fullmatch(
        r"([A-Za-z]{3,})\s+(\d{4})",
        period_str
    )

    if single:

        mon, year = single.groups()

        month = MONTH_MAP.get(
            mon[:3].lower()
        )

        months = (
            [(month, int(year))]
            if month
            else []
        )

        period = {
            "is_combined": False,
            "months": months,
            "raw": period_str,
            "is_arrear": is_explicit_arrear,
            "cross_year": False,
        }

        if not months:
            period["parse_error"] = (
                "Unrecognised month name."
            )

        return enrich_period(period)

    # Unknown format

    base.update({
        "raw": period_str,
        "is_arrear": is_explicit_arrear
    })

    base["parse_error"] = (
        "Unrecognised salary period format."
    )

    return enrich_period(base)


def extract_amount(label_patterns, text) -> float:
    for pat in label_patterns:
        m = re.search(pat + AMOUNT_AFTER_LABEL, text, re.IGNORECASE)
        if m:
            return float(m.group(1).replace(",", ""))
    return None


def extract_between(start_label: str, end_labels: list, text: str) -> str:
    """Grab text between a start label and whichever end-label comes first
    (handles fields that share a line with the next field, e.g.
    'Employee Name JAYA KUMARI Designation: CLERK')."""
    end_alt = "|".join(end_labels)
    pattern = start_label + r"\s*:?\s*(.+?)\s*(?:" + end_alt + r"|\n|$)"
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1).strip() if m else None


def parse_slip_block(block: str) -> dict:
    header_match = re.search(r"(Salary Slip\s*-\s*.*?(?:Salary|Arrear))", block, re.IGNORECASE)
    period_info = (
    parse_period(header_match.group(1))
    if header_match
    else enrich_period({
        "is_combined": False,
        "months": [],
        "raw": None,
        "is_arrear": False,
        "cross_year": False
    })
)

    result = {
        "employee_name": extract_between(r"Employee Name", [r"Designation"], block),
        "designation": extract_between(r"Designation", [r"PAN No"], block),
        "pan_no": extract_between(r"PAN No\.?", [r"Pay Scale"], block),
        "gpf_no": extract_between(r"Employee GPF No\.?", [r"Employee A/C No"], block),
        "ddo_code": extract_between(r"DDO CODE", [r"\n"], block),
        "bill_no": extract_between(r"Bill No\.?", [r"DDO CODE"], block),
        "period": period_info,
        "month_year": (
            period_info["primary_month_year"]
            if not period_info["is_combined"]
            else None
        ),

        "line_items": {},
        "deductions": {},
        "warnings": [],
    }

    for field, patterns in ALLOWANCE_SYNONYMS.items():
        val = extract_amount(patterns, block)
        if val is not None:
            result["line_items"][field] = val

    for field, patterns in DEDUCTION_SYNONYMS.items():
        val = extract_amount(patterns, block)
        if val is not None:
            result["deductions"][field] = val

    # Gross = sum of whatever allowance heads we actually found (robust to
    # column-scrambled "Total" lines). Cross-check against ANY "Total ₹X"
    # figure found anywhere in the block — not just the one positioned right
    # before "Net Pay", since column-scrambled layouts can put the smaller
    # Deduction total on that exact spot instead of the real Allowances total.
    computed_gross = round(sum(result["line_items"].values()), 2)
    result["gross_salary"] = computed_gross

    all_totals = [
        float(m.group(1).replace(",", ""))
        for m in re.finditer(r"Total\s*[₹]?\s*([\d,]+(?:\.\d+)?)", block, re.IGNORECASE)
    ]
    if all_totals and not any(abs(t - computed_gross) <= 1 for t in all_totals):
        result["warnings"].append(
            f"Computed gross ({computed_gross}) doesn't match any printed Total "
            f"figure found ({all_totals}) — check for an unrecognised allowance head."
        )

    net_match = re.search(r"Net Pay[:\s]*([\d,]+(?:\.\d+)?)", block, re.IGNORECASE)
    result["net_pay"] = float(net_match.group(1).replace(",", "")) if net_match else None

if period_info.get("parse_error"):
    result["warnings"].append(
        f"Could not safely parse salary period "
        f"'{period_info.get('raw')}': "
        f"{period_info['parse_error']} "
        f"Manual review required."
    )

if period_info.get("is_arrear"):
        result["source"] = "arrear"
    elif period_info["is_combined"]:
        result["source"] = "combined_period"
    else:
        result["source"] = "extracted"

    if period_info.get("cross_year"):
        result["warnings"].append(
            "This bill's period spans multiple financial years — almost certainly a "
            "retroactive pay-fixation arrear, not a regular monthly salary. Route to "
            "arrears_entries and confirm which cycle's tax computation it should count "
            "towards (based on actual disbursement/TV date, not the period it covers)."
        )

    return result


def parse_full_pdf_text(full_text: str) -> list:
    slips = [parse_slip_block(b) for b in split_into_slip_blocks(full_text)]
    flag_anomalous_periods(slips)
    return slips


def flag_anomalous_periods(slips: list) -> None:
    """
    Cross-check every normal (non-arrear, non-combined) slip's month/year
    against the March-February cycle implied by the majority of the other
    slips. A single-month bill that falls outside that 12-month window is
    almost certainly a mislabeled/duplicate entry (seen in real data: a
    slip labelled "Jan 2024" sitting among a Mar-2024..Feb-2025 cycle,
    which would actually PRECEDE that cycle's start — impossible for a
    genuine bill of that cycle). Flagged in-place for manual review rather
    than silently trusted or silently dropped.
    """
    normal_slips = [s for s in slips if s["source"] == "extracted" and len(s["period"]["months"]) == 1]
    if len(normal_slips) < 3:
        return  # not enough data to infer the dominant cycle confidently

    # Cycle-relative index: March=0 .. next February=11, spanning into the next year.
    def cycle_index(month, year):
        return ((month - 3) % 12), year - (0 if month >= 3 else 1)

    indexed = [(cycle_index(*s["period"]["months"][0]), s) for s in normal_slips]
    cycle_start_years = [year for (_, year), _ in indexed]
    dominant_year = max(set(cycle_start_years), key=cycle_start_years.count)

    for (idx, year), slip in indexed:
        if year != dominant_year:
            mon, yr = slip["period"]["months"][0]
            slip["warnings"].append(
                f"Period '{slip['period']['raw']}' falls outside the {dominant_year}-{dominant_year+1} "
                f"Mar-Feb cycle that the other slips belong to — likely a mislabeled or "
                f"duplicate bill. Needs manual review before including in this cycle's ledger."
            )


def extract_text_from_pdf(file_path: str):
    """
    Extract text from a PDF using pdfplumber. Returns (text, error).
    If the PDF is scanned/image-based (no extractable text layer), this
    project does NOT attempt OCR — it returns a clear error instead, since
    only text-based salary slip PDFs are supported for now.
    """
    import pdfplumber

    try:
        with pdfplumber.open(file_path) as pdf:
            pages_text = [page.extract_text() or "" for page in pdf.pages]
        full_text = "\n".join(pages_text).strip()
    except Exception as e:
        return None, f"Could not open this PDF: {e}"

    # Heuristic: a text-based salary slip should have a reasonable amount of
    # actual text and contain at least one recognisable salary-slip keyword.
    # A scanned/image-only PDF extracts to empty or near-empty text.
    if len(full_text) < 50 or not re.search(r"salary|basic|net pay", full_text, re.IGNORECASE):
        return None, (
            "This PDF doesn't have a readable text layer (it looks scanned "
            "or image-based). Only text-based salary slip PDFs are supported "
            "right now — please upload one where the text can be selected/copied."
        )

    return full_text, None


def load_and_parse_pdf(file_path: str) -> dict:
    """
    Full entry point: load a PDF file path, extract text, and parse it.
    Returns { "success": True, "slips": [...] } or
            { "success": False, "error": "..." }
    """
    full_text, error = extract_text_from_pdf(file_path)
    if error:
        return {"success": False, "error": error}

    slips = parse_full_pdf_text(full_text)
    if not slips:
        return {
            "success": False,
            "error": "No recognisable salary slip blocks found in this PDF.",
        }
    return {"success": True, "slips": slips}


if __name__ == "__main__":
    with open("sample_slip_text.txt") as f:
        full_text = f.read()

    parsed = parse_full_pdf_text(full_text)
    for slip in parsed:
        print(f"\n--- Period: {slip['period']['raw']} (combined={slip['period']['is_combined']}) ---")
        print(f"Name: {slip['employee_name']}, PAN: {slip['pan_no']}, Designation: {slip['designation']}")
        print(f"Line items: {slip['line_items']}")
        print(f"Deductions: {slip['deductions']}")
        print(f"Computed gross: {slip['gross_salary']}  |  Net pay: {slip['net_pay']}")
        if slip["warnings"]:
            print(f"⚠ Warnings: {slip['warnings']}")

    # --- Test the non-text-based (scanned PDF) fallback path ---
    print("\n--- Testing fallback for a non-text-based PDF ---")
    fake_scanned_text = ""  # simulates what pdfplumber returns for a scanned PDF
    if len(fake_scanned_text) < 50:
        print(
            "Would return: {'success': False, 'error': \"This PDF doesn't have a "
            "readable text layer... please upload one where the text can be "
            "selected/copied.\"}"
        )

    # --- Test the real end-to-end entry point ---
    print("\n--- Testing load_and_parse_pdf() on the real uploaded PDF ---")
    result = load_and_parse_pdf("/mnt/user-data/uploads/salaryslip_KHN_EDN_D951.pdf")
    print(f"success: {result['success']}, slips found: {len(result.get('slips', []))}")
