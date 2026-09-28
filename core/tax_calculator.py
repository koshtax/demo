"""
New Tax Regime calculator (FY 2025-26 slabs, as per Budget 2025).
Keep slabs + standard deduction + rebate limit configurable — these change
almost every budget, so store this config in an admin-editable DB table,
not hardcoded in production.
"""

STANDARD_DEDUCTION = 75000
REBATE_87A_MAX = 60000
REBATE_87A_LIMIT = 1200000  # taxable income (post standard deduction) up to which tax is nil
CESS_RATE = 0.04

# (upper_limit_of_slab, rate) — slabs are cumulative, last slab has no upper limit (use float('inf'))
DEFAULT_SLABS = [
    (400000, 0.00),
    (800000, 0.05),
    (1200000, 0.10),
    (1600000, 0.15),
    (2000000, 0.20),
    (2400000, 0.25),
    (float("inf"), 0.30),
]


def compute_slab_tax(taxable_income: float, slabs=DEFAULT_SLABS) -> float:
    tax = 0.0
    prev_limit = 0
    for limit, rate in slabs:
        if taxable_income > prev_limit:
            slab_amount = min(taxable_income, limit) - prev_limit
            tax += slab_amount * rate
            prev_limit = limit
        else:
            break
    return tax


def compute_annual_tax(
    annual_gross: float,
    standard_deduction: float = STANDARD_DEDUCTION,
    rebate_limit: float = REBATE_87A_LIMIT,
    rebate_max: float = REBATE_87A_MAX,
    slabs=DEFAULT_SLABS,
    cess_rate: float = CESS_RATE,
) -> dict:
    """
    FY 2025-26 / AY 2026-27 New Tax Regime calculation.

    Includes:
    - Standard deduction
    - Section 87A rebate
    - Marginal relief just above the rebate threshold
    - Health & Education Cess
    """

    try:
        annual_gross = float(annual_gross)
        standard_deduction = float(standard_deduction)
        rebate_limit = float(rebate_limit)
        rebate_max = float(rebate_max)
        cess_rate = float(cess_rate)
    except (TypeError, ValueError):
        raise ValueError("Tax calculation inputs must be numeric.")

    if annual_gross < 0:
        raise ValueError("Annual gross cannot be negative.")

    taxable_income = max(0.0, annual_gross - standard_deduction)
    slab_tax = compute_slab_tax(taxable_income, slabs)

    rebate_amount = 0.0
    marginal_relief = 0.0

    if taxable_income <= rebate_limit:
        rebate_amount = min(slab_tax, rebate_max)
        tax_after_rebate = max(0.0, slab_tax - rebate_amount)
    else:
        excess_income = taxable_income - rebate_limit

        if slab_tax > excess_income:
            marginal_relief = slab_tax - excess_income
            tax_after_rebate = excess_income
        else:
            tax_after_rebate = slab_tax

    cess = tax_after_rebate * cess_rate
    total_tax_payable = round(tax_after_rebate + cess)

    return {
        "annual_gross": round(annual_gross, 2),
        "standard_deduction": round(standard_deduction, 2),
        "taxable_income": round(taxable_income, 2),
        "slab_tax": round(slab_tax, 2),
        "rebate_applied": rebate_amount > 0,
        "rebate_amount": round(rebate_amount, 2),
        "marginal_relief_applied": marginal_relief > 0,
        "marginal_relief": round(marginal_relief, 2),
        "tax_after_rebate": round(tax_after_rebate, 2),
        "cess": round(cess, 2),
        "total_tax_payable": total_tax_payable,
    }


if __name__ == "__main__":
    # --- Test with real sample data: JAYA KUMARI ---
    # Monthly gross (Nov/Dec/Jan pattern) = 33,932 -> projected annual approx
    monthly_gross = 33932
    projected_annual_gross = monthly_gross * 12
    result = compute_annual_tax(projected_annual_gross)
    print(f"Projected annual gross: {projected_annual_gross}")
    print(result)
    # Expect: taxable income well under 4L after standard deduction -> zero tax
