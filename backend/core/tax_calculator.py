"""
New Tax Regime calculator (FY 2025-26 slabs, as per Budget 2025).
Keep slabs + standard deduction + rebate limit configurable — these change
almost every budget, so store this config in an admin-editable DB table,
not hardcoded in production.
"""

STANDARD_DEDUCTION = 75000
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
    slabs=DEFAULT_SLABS,
    cess_rate: float = CESS_RATE,
) -> dict:
    """
    Returns a breakdown dict: taxable_income, slab_tax, rebate_applied,
    tax_after_rebate, cess, total_tax_payable.

    NOTE: marginal relief near the exact rebate boundary (income just above
    12,00,000) is NOT implemented here — flag this as a follow-up if slips
    with income close to that boundary are expected.
    """
    taxable_income = max(0, annual_gross - standard_deduction)
    slab_tax = compute_slab_tax(taxable_income, slabs)

    if taxable_income <= rebate_limit:
        tax_after_rebate = 0.0
    else:
        tax_after_rebate = slab_tax

    cess = tax_after_rebate * cess_rate
    total_tax_payable = round(tax_after_rebate + cess)

    return {
        "taxable_income": taxable_income,
        "slab_tax": round(slab_tax),
        "rebate_applied": taxable_income <= rebate_limit,
        "tax_after_rebate": round(tax_after_rebate),
        "cess": round(cess),
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
