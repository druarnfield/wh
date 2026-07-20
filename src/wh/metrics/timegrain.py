"""Time grain expressions: one period-START date per grain, fiscal included.

Fiscal periods use the shift trick — slide dates back by (fiscal_year_start
- 1) months, truncate on the calendar boundary, slide forward again — so
`fy`/`fy_quarter` need no calendar table. The result type is DATE always.
"""

from __future__ import annotations

from datetime import date

from ..errors import SemanticsError

GRAINS = ("day", "week", "month", "quarter", "year", "fy", "fy_quarter")

# one period's width, as a SQL interval (compare shifting, completeness)
PERIOD_INTERVAL = {
    "day": "1 DAY", "week": "7 DAY", "month": "1 MONTH", "quarter": "3 MONTH",
    "year": "1 YEAR", "fy": "1 YEAR", "fy_quarter": "3 MONTH",
}


def fy_start(d: date, fiscal_year_start: int) -> date:
    """First day of the fiscal year containing `d` (pure-Python mirror of
    the SQL shift trick)."""
    y = d.year if d.month >= fiscal_year_start else d.year - 1
    return date(y, fiscal_year_start, 1)


def grain_expr(grain: str, column_sql: str, fiscal_year_start: int) -> str:
    """DuckDB expression for the period-start date of `column_sql` at `grain`."""
    if grain not in GRAINS:
        raise SemanticsError(
            f"unknown grain '{grain}' — valid grains: {', '.join(GRAINS)}"
        )
    if grain in ("fy", "fy_quarter"):
        base = "year" if grain == "fy" else "quarter"
        shift = fiscal_year_start - 1
        if shift == 0:
            grain = base
        else:
            return (
                f"CAST(date_trunc('{base}', {column_sql} - INTERVAL {shift} MONTH)"
                f" + INTERVAL {shift} MONTH AS DATE)"
            )
    return f"CAST(date_trunc('{grain}', {column_sql}) AS DATE)"
