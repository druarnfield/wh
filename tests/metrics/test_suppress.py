"""Small-cell suppression: cells below n go NULL, dependent ratios too."""

from datetime import date

from wh.metrics.compiler import compile_slice
from wh.metrics.context_ops import context
from wh.metrics.result import Slice


def run(con, compiled):
    rows = con.execute(compiled.sql).fetchall()
    cols = [d[0] for d in con.description]
    return [dict(zip(cols, r)) for r in rows]


JUNE = context(time=("2026-06-01", "2026-06-30"))


def test_small_cells_are_nulled(con, defs):
    rows = run(con, compile_slice(
        defs["waitlist"], ["patients_waiting"], by=["facility.region"],
        ctx=JUNE, suppress=5,
    ))
    assert {r["facility.region"]: r["patients_waiting"] for r in rows} == {
        "North": None, "South": None,          # 3 and 1, both under 5
    }
    rows = run(con, compile_slice(
        defs["waitlist"], ["patients_waiting"], by=["facility.region"],
        ctx=JUNE, suppress=2,
    ))
    assert {r["facility.region"]: r["patients_waiting"] for r in rows} == {
        "North": 3, "South": None,
    }


def test_ratio_suppressed_when_its_denominator_is_small(con, make_defs, design_yaml):
    """A big cell can still have a tiny denominator — den < n nulls the
    ratio even when the cell itself survives."""
    yaml = design_yaml["waitlist"].replace(
        "        den: count(*)\n",
        "        den: count(*) FILTER (WHERE wait_days > 100)\n",
    )
    m = make_defs(design_yaml["dims"], yaml)["waitlist"]
    rows = run(con, compile_slice(
        m, ["pct_over_target"], by=["facility.region"], ctx=JUNE, suppress=3,
    ))
    got = {r["facility.region"]: r["pct_over_target"] for r in rows}
    assert got["North"] is None                # cell_n 3 >= 3, but den 2 < 3


def test_compare_columns_are_suppressed_from_their_own_cells(con, defs):
    rows = run(con, compile_slice(
        defs["removals"], ["removals"], by=["facility.region"],
        grain="month", compare=["prior"], suppress=2,
    ))
    got = {(r["period"], r["facility.region"]): r for r in rows}
    july = date(2026, 7, 1)
    assert got[(july, "North")]["removals"] is None            # July North: 1 row
    assert got[(july, "North")]["removals_prior"] == 2          # June North: 3 rows
    assert got[(july, "South")]["removals"] == 1                # 2 rows, survives


def test_suppress_returns_a_new_slice(defs, con):
    s = Slice(defs["removals"], ["removals"], con=lambda: con)
    s2 = s.suppress(2)
    assert "__cell_n" not in s.sql
    assert "__cell_n" in s2.sql and "CASE WHEN" in s2.sql
    assert s2 is not s
