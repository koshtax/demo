def build_full_matrix() -> dict:
    """
    Complete 7th CPC civilian Pay Matrix.

    Level 13 uses the revised matrix effective from 01-01-2016.
    Vertical progression is 3%, rounded to the nearest Rs. 100.
    """

    level_limits = {
        1: (18000, 56900),
        2: (19900, 63200),
        3: (21700, 69100),
        4: (25500, 81100),
        5: (29200, 92300),
        6: (35400, 112400),
        7: (44900, 142400),
        8: (47600, 151100),
        9: (53100, 167800),
        10: (56100, 177500),
        11: (67700, 208700),
        12: (78800, 209200),
        13: (123100, 215900),
        "13A": (131100, 216600),
        14: (144200, 218200),
        15: (182200, 224100),
        16: (205400, 224400),
        17: (225000, 225000),
        18: (250000, 250000),
    }

    matrix = {}

    for level, (start_pay, max_pay) in level_limits.items():
        cells = [start_pay]
        current_pay = start_pay

        while current_pay < max_pay:
            next_pay = int((current_pay * 1.03 + 50) // 100) * 100

            if next_pay > max_pay:
                break

            cells.append(next_pay)
            current_pay = next_pay

        matrix[level] = cells

    return matrix
def find_level_and_cell(basic_pay: float, matrix: dict):
    """
    Basic Pay ko 7th CPC matrix me EXACT match karta hai.

    Returns:
        (level, cell_index)

    Example:
        49000 -> matching level/cell
        invalid amount -> (None, None)

    IMPORTANT:
    Nearest/lower cell guess nahi kiya jayega.
    """

    if basic_pay is None:
        return None, None

    try:
        basic_pay = float(basic_pay)
    except (TypeError, ValueError):
        return None, None

    for level, cells in matrix.items():
        for cell_index, cell_value in enumerate(cells):
            if float(cell_value) == basic_pay:
                return level, cell_index

    return None, None
def next_cell(level: int, current_basic: float, matrix: dict) -> float:
    """
    Given Pay Level me current Basic ka next increment cell return karta hai.

    Current Basic ka EXACT cell match zaroori hai.
    Agar level/basic invalid ho ya employee already last available cell par ho,
    current Basic unchanged return hoga.
    """

    cells = matrix.get(level, [])

    if not cells:
        return current_basic

    try:
        current_basic = float(current_basic)
    except (TypeError, ValueError):
        return current_basic

    for cell_index, cell_value in enumerate(cells):
        if float(cell_value) == current_basic:
            if cell_index + 1 < len(cells):
                return cells[cell_index + 1]

            return current_basic

    return current_basic
def correct_basic_pay(level: int, raw_basic: float, matrix: dict, source: str = "extracted") -> float:
    """
    Dynamic matrix snap logic.
    Bypasses matrix forcing if the salary slip is a combined period or arrear bill.
    """
    # Dynamic bypass for anomalies
    if source in ["combined_period", "arrear"]:
        return raw_basic  # As-is gross ke liye chhod do
        
    cells = matrix.get(level, [])
    if not cells:
        return raw_basic
        
    valid_below = [c for c in cells if c <= raw_basic]
    # Agar arrear galti se normal mark ho gaya aur chota hai, toh original return karo (deflation bug fix)
    if not valid_below:
        return raw_basic 
        
    return max(valid_below)
