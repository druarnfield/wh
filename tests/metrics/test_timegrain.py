from datetime import date

import duckdb
import pytest

from wh.errors import SemanticsError
from wh.metrics.timegrain import grain_expr


def evaluate(expr: str, day: str):
    con = duckdb.connect()
    sql = expr.replace("__col__", f"DATE '{day}'")
    return con.execute(f"SELECT {sql}").fetchone()[0]


@pytest.mark.parametrize(
    "grain,day,expected",
    [
        ("day", "2025-08-15", date(2025, 8, 15)),
        ("week", "2025-08-15", date(2025, 8, 11)),      # Monday
        ("month", "2025-08-15", date(2025, 8, 1)),
        ("quarter", "2025-08-15", date(2025, 7, 1)),
        ("year", "2025-08-15", date(2025, 1, 1)),
        # fiscal, July start (AU health)
        ("fy", "2025-08-15", date(2025, 7, 1)),
        ("fy", "2025-03-10", date(2024, 7, 1)),
        ("fy_quarter", "2025-08-15", date(2025, 7, 1)),
        ("fy_quarter", "2025-11-02", date(2025, 10, 1)),
        ("fy_quarter", "2025-03-10", date(2025, 1, 1)),
    ],
)
def test_grain_period_starts(grain, day, expected):
    assert evaluate(grain_expr(grain, "__col__", 7), day) == expected


def test_calendar_fiscal_degenerates_to_year_and_quarter():
    assert grain_expr("fy", "c", 1) == grain_expr("year", "c", 1)
    assert grain_expr("fy_quarter", "c", 1) == grain_expr("quarter", "c", 1)


def test_unknown_grain_names_the_valid_set():
    with pytest.raises(SemanticsError, match="fy_quarter"):
        grain_expr("fortnight", "c", 7)


def test_period_start_mirrors_the_engine_exactly():
    """period_start IS grain_expr in Python (provenance arithmetic) — walk
    a dense multi-year date grid at several fiscal starts and hold the two
    cell-exact equal. Mutation tripwire for every branch of period_start."""
    from datetime import timedelta

    from wh.metrics.timegrain import GRAINS, period_start

    con = duckdb.connect()
    days = sorted(date(2024, 1, 1) + timedelta(days=13 * i) for i in range(85))
    con.execute("CREATE TABLE days (d DATE)")
    con.executemany("INSERT INTO days VALUES (?)", [(d,) for d in days])
    for fys in (1, 7, 10):
        for grain in GRAINS:
            got = [r[0] for r in con.execute(
                f"SELECT {grain_expr(grain, 'd', fys)} FROM days ORDER BY d"
            ).fetchall()]
            want = [period_start(d, grain, fys) for d in days]
            assert got == want, (grain, fys)


def test_fy_start_boundary_month_opens_the_new_fy():
    from wh.metrics.timegrain import fy_start

    assert fy_start(date(2026, 7, 1), 7) == date(2026, 7, 1)
    assert fy_start(date(2026, 6, 30), 7) == date(2025, 7, 1)
    assert fy_start(date(2026, 12, 31), 7) == date(2026, 7, 1)
