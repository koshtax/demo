import os
from datetime import datetime

from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML, CSS

from core.tax_calculator import compute_annual_tax


TEMPLATE_DIR = "templates"
TEMPLATE_NAME = "form16_template.html"


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


def _number(value):
    """Safely convert DB/template values to float."""
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _money(value):
    return f"{_number(value):.2f}"


def _assessment_year(financial_year):
    """
    FY 2026-27 -> AY 2027-28
    """
    start_year = int(financial_year[:4])

    return (
        f"{start_year + 1}-"
        f"{(start_year + 2) % 100:02d}"
    )


def _deduction_value(deductions, *names):
    """
    Supports canonical names plus older aliases while
    templates are being migrated.
    """
    deductions = deductions or {}

    for name in names:
        if name in deductions:
            return _number(deductions.get(name))

    return 0.0


def _line_item_value(line_items, *names):
    line_items = line_items or {}

    for name in names:
        if name in line_items:
            return _number(line_items.get(name))

    return 0.0


def _prepare_ledger(ledger):
    """
    Convert database ledger dictionaries into the field names
    currently expected by form16_template.html.

    No salary projection happens here.
    """

    formatted = []

    totals = {
        "basic": 0.0,
        "da": 0.0,
        "hra": 0.0,
        "ta": 0.0,
        "medical": 0.0,
        "gross": 0.0,
        "gpf": 0.0,
        "gli": 0.0,
        "prof_tax": 0.0,
        "tds": 0.0,
        "total_deduction": 0.0,
        "net_pay": 0.0,
    }

    for row in ledger:
        month = int(row.get("month", 0) or 0)
        year = int(row.get("year", 0) or 0)

        line_items = row.get("line_items") or {}
        deductions = row.get("deductions") or {}

        basic = _number(row.get("basic_pay"))
        da = _number(row.get("da"))
        hra = _number(row.get("hra"))

        ta = _line_item_value(
            line_items,
            "ta",
            "travel_allowance",
            "transport_allowance",
        )

        medical = _line_item_value(
            line_items,
            "medical",
            "medical_allowance",
        )

        gross = _number(row.get("gross_salary"))

        # Do not silently drop dynamic earnings if gross is absent.
        if gross <= 0:
            gross = sum(
                _number(value)
                for value in line_items.values()
                if isinstance(value, (int, float))
            )

            # Older/manual ledger rows may contain only dynamic
            # extras in line_items_json.
            if gross <= 0:
                gross = basic + da + hra + ta + medical

        gpf = _deduction_value(
            deductions,
            "gpf",
        )

        gli = _deduction_value(
            deductions,
            "gli",
            "gis",
        )

        prof_tax = _deduction_value(
            deductions,
            "professional_tax",
            "prof_tax",
        )

        tds = _deduction_value(
            deductions,
            "income_tax_tds",
            "tds",
            "tax",
        )

        total_deduction = sum(
            _number(value)
            for value in deductions.values()
            if isinstance(value, (int, float))
        )

        net_pay = gross - total_deduction

        month_name = MONTH_NAMES.get(
            month,
            f"Month {month}",
        )

        if row.get("is_auto_generated"):
            month_name += " (Auto)"

        formatted.append(
            {
                "month": month,
                "year": year,
                "month_year": row.get("month_year"),
                "month_name": month_name,

                "basic": _money(basic),
                "da": _money(da),
                "hra": _money(hra),
                "ta": _money(ta),
                "medical": _money(medical),

                "gross": _money(gross),

                "gpf": _money(gpf),
                "gli": _money(gli),
                "gis": _money(gli),

                "prof_tax": _money(prof_tax),
                "tds": _money(tds),

                "total_deduction": _money(
                    total_deduction
                ),

                "net_pay": _money(net_pay),

                "source": row.get("source", ""),
                "note": row.get("note"),
                "flags": row.get("flags") or [],
                "is_auto_generated": bool(
                    row.get("is_auto_generated")
                ),
            }
        )

        totals["basic"] += basic
        totals["da"] += da
        totals["hra"] += hra
        totals["ta"] += ta
        totals["medical"] += medical
        totals["gross"] += gross

        totals["gpf"] += gpf
        totals["gli"] += gli
        totals["prof_tax"] += prof_tax
        totals["tds"] += tds

        totals["total_deduction"] += (
            total_deduction
        )

        totals["net_pay"] += net_pay

    return formatted, totals


def generate_form16_pdf(
    employee,
    employer,
    ledger,
    financial_year,
    output_filename=None,
):
    """
    Render Form 16 using already-finalised ledger data.

    Important:
    - Financial year comes from admin-selected FY.
    - No January/February projection is performed here.
    - No pay-matrix calculation is performed here.
    - Actual ledger values are the rendering source.
    """

    if not financial_year:
        raise ValueError(
            "Financial year is required."
        )

    if not employee:
        raise ValueError(
            "Employee details are required."
        )

    if not employer:
        raise ValueError(
            "Employer details are required."
        )

    if not ledger:
        raise ValueError(
            "Salary ledger is empty."
        )

    assessment_year = _assessment_year(
        financial_year
    )

    formatted_ledger, totals = _prepare_ledger(
        ledger
    )

    # Tax calculator remains the single source of truth
    # for annual tax computation.
    tax_result = compute_annual_tax(
        totals["gross"]
    )

    standard_deduction = _number(
        tax_result.get(
            "standard_deduction",
            75000,
        )
    )

    taxable_income = _number(
        tax_result.get(
            "taxable_income",
            max(
                totals["gross"]
                - standard_deduction,
                0,
            ),
        )
    )

    income_tax = _number(
        tax_result.get(
            "tax_after_rebate",
            tax_result.get(
                "income_tax",
                tax_result.get("tax", 0),
            ),
        )
    )

    cess = _number(
        tax_result.get(
            "cess",
            tax_result.get(
                "health_education_cess",
                0,
            ),
        )
    )

    total_tax_payable = _number(
        tax_result.get(
            "total_tax",
            tax_result.get(
                "total_tax_payable",
                income_tax + cess,
            ),
        )
    )

    rebate = _number(
        tax_result.get(
            "rebate_87a",
            tax_result.get("rebate", 0),
        )
    )

    marginal_relief = _number(
        tax_result.get(
            "marginal_relief",
            0,
        )
    )

    employee_name = (
        getattr(employee, "name", None)
        or ""
    )

    office_name = (
        getattr(
            employee,
            "office_school_name",
            None,
        )
        or ""
    )

    employee_pan = (
        getattr(employee, "pan", None)
        or ""
    )

    employer_name = (
        getattr(employer, "employer_name", None)
        or ""
    )

    employer_address = (
        getattr(
            employer,
            "employer_address",
            None,
        )
        or ""
    )

    employer_name_address = ", ".join(
        part
        for part in (
            employer_name,
            employer_address,
        )
        if part
    )

    # Current schema stores the responsible DDO's name
    # and father name on EmployerCache.
    officer_name = (
        getattr(
            employer,
            "officer_name",
            None,
        )
        or ""
    )

    officer_father_name = (
        getattr(
            employer,
            "officer_father_name",
            None,
        )
        or ""
    )

    designation = (
        getattr(
            employer,
            "designation",
            None,
        )
        or ""
    )

    template_vars = {
        # -----------------------------------------
        # FY / AY
        # -----------------------------------------
        "financial_year": financial_year,
        "assessment_year": assessment_year,

        # -----------------------------------------
        # Employee
        # -----------------------------------------
        "employee_name": employee_name,
        "pan": employee_pan,
        "office_name": office_name,

        # EmployeeDetail currently has no employee
        # father-name column. Keep blank rather than
        # inventing a person's name.
        "father_name": "",

        # Current schema has DDO designation, not a
        # separate employee-designation field.
        "designation": "",

        # -----------------------------------------
        # Employer / DDO
        # -----------------------------------------
        "employer_name": employer_name,
        "employer_address": employer_address,

        "employer_name_address": (
            employer_name_address
        ),

        "tan": (
            getattr(employer, "tan", None)
            or ""
        ),

        "employer_pan": (
            getattr(employer, "pan", None)
            or ""
        ),

        "officer_name": officer_name,

        "officer_father_name": (
            officer_father_name
        ),

        "ddo_designation": designation,

        # -----------------------------------------
        # Ledger
        # -----------------------------------------
        "ledger_data": formatted_ledger,

        # -----------------------------------------
        # Salary totals
        # -----------------------------------------
        "salary_amount": _money(
            totals["basic"]
        ),

        "basic_amount": _money(
            totals["basic"]
        ),

        "da_amount": _money(
            totals["da"]
        ),

        "hra_amount": _money(
            totals["hra"]
        ),

        "ta_amount": _money(
            totals["ta"]
        ),

        "medical_amount": _money(
            totals["medical"]
        ),

        "gross_salary": _money(
            totals["gross"]
        ),

        "gross_total_income": _money(
            totals["gross"]
        ),

        # -----------------------------------------
        # Deductions
        # -----------------------------------------
        "total_gpf": _money(
            totals["gpf"]
        ),

        "total_gli": _money(
            totals["gli"]
        ),

        # Compatibility with existing template.
        "total_gis": _money(
            totals["gli"]
        ),

        "total_prof_tax": _money(
            totals["prof_tax"]
        ),

        "total_tds": _money(
            totals["tds"]
        ),

        "q1_tds": _money(
            totals["tds"]
        ),

        "challan_tax": _money(
            totals["tds"]
        ),

        "tds_paid": _money(
            totals["tds"]
        ),

        "total_deduction": _money(
            totals["total_deduction"]
        ),

        "net_pay": _money(
            totals["net_pay"]
        ),

        # Existing template compatibility.
        "total_80c": _money(
            totals["gpf"] + totals["gli"]
        ),

        "total_80c_deductible": _money(
            totals["gpf"] + totals["gli"]
        ),

        # -----------------------------------------
        # Tax
        # -----------------------------------------
        "standard_deduction": _money(
            standard_deduction
        ),

        "taxable_income": _money(
            taxable_income
        ),

        "income_tax": _money(
            income_tax
        ),

        "rebate_87a": _money(
            rebate
        ),

        "marginal_relief": _money(
            marginal_relief
        ),

        "cess": _money(
            cess
        ),

        "total_tax_payable": _money(
            total_tax_payable
        ),

        # Full calculator result remains available
        # to the template for later template cleanup.
        "tax_data": tax_result,

        # -----------------------------------------
        # Misc
        # -----------------------------------------
        "generated_on": (
            datetime.now().strftime(
                "%d-%m-%Y"
            )
        ),
    }

    env = Environment(
        loader=FileSystemLoader(
            TEMPLATE_DIR
        ),
        autoescape=True,
    )

    template = env.get_template(
        TEMPLATE_NAME
    )

    rendered_html = template.render(
        template_vars
    )

    if output_filename is None:

        safe_employee = "".join(
            char
            if char.isalnum()
            else "_"
            for char in (
                employee_name
                or "Employee"
            )
        ).strip("_")

        output_filename = (
            f"Form16_{safe_employee}_"
            f"FY_{financial_year}.pdf"
        )

    output_filename = os.path.abspath(
        output_filename
    )

    HTML(
        string=rendered_html,
        base_url=os.getcwd(),
    ).write_pdf(
        output_filename,
        stylesheets=[
            CSS(
                string="""
                @page {
                    size: A4 portrait;
                    margin: 0.5in;
                }

                @page landscape_page {
                    size: A4 landscape;
                    margin: 0.5in;
                }

                .landscape-section,
                .landscape {
                    page: landscape_page;
                }

                body {
                    font-family: Helvetica, sans-serif;
                    font-size: 11px;
                }

                table {
                    width: 100%;
                    border-collapse: collapse;
                }

                th,
                td {
                    border: 1px solid black;
                    padding: 4px;
                }

                .no-border {
                    border: none;
                }

                .text-right {
                    text-align: right;
                }

                .text-center {
                    text-align: center;
                }

                .bold {
                    font-weight: bold;
                }

                .page-break {
                    page-break-before: always;
                }
                """
            )
        ],
    )

    return output_filename
