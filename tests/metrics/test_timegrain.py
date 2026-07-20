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
