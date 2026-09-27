"""
Generic allowance-pattern classifier.

Different allowance heads behave differently, and it varies by department/
city/state — TA might be a flat amount in one office and unheard-of in
another; DA/HRA are almost always %-of-basic but the % itself can change
independently of basic (DA rate revisions happen twice a year, unrelated
to pay-matrix increments). So instead of hardcoding "DA/HRA = %, Medical =
flat, TA = ???", this classifies each allowance head from the employee's
own history:

  1. Find month-pairs where basic_pay actually changed (an increment).
  2. For each such pair, check what happened to the allowance:
       - value stayed exactly the same          -> "flat"
       - value/basic ratio stayed the same      -> "percentage"
       - value changed, but not proportionally  -> "dynamic"
  3. Only basic-CHANGE transitions are used for this — a DA rate hike that
     happens on its own (basic unchanged) is correctly ignored here, so it
     doesn't get mistaken for "dynamic" just because the % moved that month
     for reasons unrelated to the increment.

If no increment has happened yet in the employee's history, there isn't
enough evidence to classify -> "insufficient_data". Auto-generation should
then fall back to "carry forward last known value as-is" AND flag it for
manual review, rather than guessing.
"""

from typing import List, Dict, Optional

RATIO_TOLERANCE = 0.02  # 2% relative tolerance when comparing before/after ratios

# Only for allowance heads that are near-universal across Indian govt salary
# structures (DA and HRA are governed by fixed formulas — %-of-basic — under
# central/state rules; Medical is conventionally a flat sum). These act as a
# FALLBACK only when there's no actual increment-transition evidence yet
# (e.g. generating this cycle's very first increment for an employee with no
# prior-year history on file). Anything NOT in this dict — TA, or any other
# department/city-specific head — gets NO assumed default: it is classified
# purely from evidence, and stays "insufficient_data" (manual review) until
# an actual increment is observed for that employee.
KNOWN_STANDARD_DEFAULTS = {
    "da": "percentage",
    "hra": "percentage",
    "medical": "flat",
}


def find_basic_change_transitions(history: List[Dict]) -> List[tuple]:
    """history must be chronologically sorted. Returns (before, after) pairs
    for every consecutive month where basic_pay differs."""
    transitions = []
    for i in range(1, len(history)):
        prev, curr = history[i - 1], history[i]
        if prev.get("basic_pay") != curr.get("basic_pay"):
            transitions.append((prev, curr))
    return transitions


def classify_field(field_name: str, history: List[Dict], reference_history: List[Dict] = None,
                    tolerance: float = RATIO_TOLERANCE, use_known_defaults: bool = True) -> str:
    """
    Returns one of: "percentage", "flat", "dynamic", "insufficient_data".

    Looks for increment-transition evidence first in `history` (this cycle),
    then in `reference_history` (e.g. last cycle's data, if available) —
    real evidence always wins. Only when NEITHER has a usable transition
    does it fall back to KNOWN_STANDARD_DEFAULTS (and only for fields in
    that table — TA and any other non-standard head still comes back
    "insufficient_data" with no data, exactly as it should).
    """
    for source in (history, reference_history or []):
        transitions = find_basic_change_transitions(source)
        for before, after in reversed(transitions):  # most recent first
            v_before = before.get(field_name)
            v_after = after.get(field_name)
            if v_before is None or v_after is None:
                continue

            if v_before == v_after:
                return "flat"

            b_before = before.get("basic_pay")
            b_after = after.get("basic_pay")
            if not b_before or not b_after:
                continue

            ratio_before = v_before / b_before
            ratio_after = v_after / b_after
            if abs(ratio_after - ratio_before) <= tolerance * ratio_before:
                return "percentage"

            return "dynamic"

    if use_known_defaults and field_name in KNOWN_STANDARD_DEFAULTS:
        return KNOWN_STANDARD_DEFAULTS[field_name]

    return "insufficient_data"


def classify_all_fields(history: List[Dict], reference_history: List[Dict] = None,
                         exclude=("basic_pay", "month", "year", "source")) -> Dict[str, str]:
    """Classify every allowance field seen anywhere in the history (skipping
    structural keys like basic_pay/month/year/source)."""
    all_fields = set()
    for month in history:
        all_fields.update(k for k in month.keys() if k not in exclude)

    return {field: classify_field(field, history, reference_history) for field in sorted(all_fields)}


def project_field_forward(field_name: str, pattern: str, last_known: dict, new_basic: float) -> Optional[float]:
    """Given a classified pattern, project this field's value onto a new
    (incremented) basic pay. Returns None for 'dynamic'/'insufficient_data'
    — those cases should NOT be auto-filled; carry forward the last known
    raw value instead and flag for manual review."""
    old_value = last_known.get(field_name)
    old_basic = last_known.get("basic_pay")

    if pattern == "flat":
        return old_value
    if pattern == "percentage" and old_basic:
        return round(new_basic * (old_value / old_basic), 2)
    return None  # dynamic / insufficient_data -> caller must flag for manual review


if __name__ == "__main__":
    # --- Real data test: Vivek Kumar Pandey, Mar'25-Feb'26 monthly ledger ---
    # (Basic increments once, in January, from 49000 -> 50500)
    history = [
        {"month": 3, "year": 2025, "basic_pay": 49000, "da": 25970, "hra": 4900, "medical": 500},
        {"month": 4, "year": 2025, "basic_pay": 49000, "da": 25970, "hra": 4900, "medical": 500},
        {"month": 5, "year": 2025, "basic_pay": 49000, "da": 26950, "hra": 4900, "medical": 500},
        {"month": 6, "year": 2025, "basic_pay": 49000, "da": 26950, "hra": 4900, "medical": 500},
        {"month": 7, "year": 2025, "basic_pay": 49000, "da": 26950, "hra": 4900, "medical": 500},
        {"month": 8, "year": 2025, "basic_pay": 49000, "da": 26950, "hra": 4900, "medical": 500},
        {"month": 9, "year": 2025, "basic_pay": 49000, "da": 26950, "hra": 4900, "medical": 500},
        {"month": 10, "year": 2025, "basic_pay": 49000, "da": 28420, "hra": 4900, "medical": 500},
        {"month": 11, "year": 2025, "basic_pay": 49000, "da": 28420, "hra": 4900, "medical": 500},
        {"month": 12, "year": 2025, "basic_pay": 49000, "da": 28420, "hra": 4900, "medical": 500},
        {"month": 1, "year": 2026, "basic_pay": 50500, "da": 29290, "hra": 5050, "medical": 500},
        {"month": 2, "year": 2026, "basic_pay": 50500, "da": 29290, "hra": 5050, "medical": 500},
    ]

    classifications = classify_all_fields(history)
    print("Classified patterns:", classifications)
    # Expect: da -> percentage, hra -> percentage, medical -> flat
    # (Note: DA's % itself moved 53%->55%->58% across Mar-Dec purely from DA
    #  rate revisions, basic unchanged in those months — correctly ignored
    #  since only the Dec->Jan basic-change transition is used to decide.)

    # --- Now verify: does projecting forward from December reproduce the
    #     ACTUAL January values we already have? (ground-truth check) ---
    december = history[9]
    new_basic = 50500
    print("\nProjecting Dec -> Jan (checking against real Jan values):")
    for field in ["da", "hra", "medical"]:
        pattern = classifications[field]
        projected = project_field_forward(field, pattern, december, new_basic)
        actual = history[10][field]
        match = "✓" if projected == actual else "✗ MISMATCH"
        print(f"  {field}: pattern={pattern}, projected={projected}, actual={actual}  {match}")

    # --- TA test: no data available (this employee has none) ---
    print("\nTA with no historical data:", classify_field("ta", []))
