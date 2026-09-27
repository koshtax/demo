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
