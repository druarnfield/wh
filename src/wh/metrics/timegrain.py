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


_PERIOD_MONTHS = {"month": 1, "quarter": 3, "year": 12, "fy": 12, "fy_quarter": 3}


def _month_add(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(y, m + 1, 1)


def period_start(d: date, grain: str, fiscal_year_start: int) -> date:
    """Pure-Python mirror of grain_expr for provenance arithmetic."""
    from datetime import timedelta

    if grain == "day":
        return d
    if grain == "week":
        return d - timedelta(days=d.weekday())
    if grain == "month":
        return d.replace(day=1)
    if grain == "quarter":
        return date(d.year, ((d.month - 1) // 3) * 3 + 1, 1)
    if grain == "year":
        return date(d.year, 1, 1)
    if grain == "fy":
        return fy_start(d, fiscal_year_start)
    if grain == "fy_quarter":
        shift = fiscal_year_start - 1
        s = _month_add(d.replace(day=1), -shift)
        q = date(s.year, ((s.month - 1) // 3) * 3 + 1, 1)
        return _month_add(q, shift)
    raise SemanticsError(f"unknown grain '{grain}' — valid grains: {', '.join(GRAINS)}")


def period_end(d: date, grain: str, fiscal_year_start: int) -> date:
    """Last day of the period containing `d`."""
    from datetime import timedelta

    if grain == "day":
        return d
    s = period_start(d, grain, fiscal_year_start)
    if grain == "week":
        return s + timedelta(days=6)
    return _month_add(s, _PERIOD_MONTHS[grain]) - timedelta(days=1)


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
