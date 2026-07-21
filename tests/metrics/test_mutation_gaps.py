"""Killing tests for mutation-run survivors (docs/testing.md ledger).

Each test pins a behavior a surviving mutant proved untested: deterministic
fixed-case differentials (fytd lane internals, shifted compare windows,
suppression of comparison columns, completeness under window truncation)
plus unit pins on the small pure helpers. Non-fuzz on purpose — the
mutation runner excludes fuzz tests."""

from datetime import date, datetime, timedelta

import duckdb
import pytest

from wh.errors import SemanticsError
from wh.metrics.compiler import (
    TimeWindow, _lit, _predicate, _time_predicate, _to_window,
    compile_slice, compile_values, split_context,
)
from wh.metrics.context_ops import All, Between, Eq

from oracle import (apply_suppress, assert_maps_equal, compare_oracle,
                    complete_filter, result_map)
from strategies import Case, MEASURES, SliceArgs, build_model, seed, to_wh_context

DIMS = (("K1", "North"), ("K2", "South"), ("K3", "North"))


def run_slice(case, args):
    con = duckdb.connect()
    seed(con, case)
    compiled = compile_slice(
        build_model(case), list(args.measures), by=list(args.by),
        ctx=to_wh_context(args), grain=args.grain, compare=list(args.compare),
        complete_periods=args.complete_periods, suppress=args.suppress,
    )
    return result_map(con.execute(compiled.sql), args.by, args.measures,
                      args.grain, compare=args.compare)


# --- unit pins on the pure helpers ---

def test_lit_renders_every_scalar_shape():
    assert _lit(True) == "TRUE"
    assert _lit(False) == "FALSE"
    assert _lit(3) == "3"
    assert _lit("plain") == "'plain'"
    assert _lit("O'Brien") == "'O''Brien'"
    assert _lit("a\x00b") == "('a' || chr(0) || 'b')"
    assert _lit("\x00") == "('' || chr(0) || '')"


def test_predicate_compiles_attribute_between():
    assert _predicate("fact.v", Between(1, 5)) == "fact.v BETWEEN 1 AND 5"


def test_time_predicate_all_is_no_predicate():
    assert _time_predicate("fact.d", All()) is None


def test_to_window_bounds_are_exact():
    """Dates: half-open at hi + 1 day. Datetimes: exact instants —
    hi_exc is the next representable microsecond, nothing more."""
    assert _to_window(Between(date(2026, 6, 1), date(2026, 6, 30))) == \
        TimeWindow(date(2026, 6, 1), date(2026, 7, 1))
    lo = datetime(2026, 6, 1, 3, 0)
    hi = datetime(2026, 6, 30, 21, 0)
    assert _to_window(Between(lo, hi)) == \
        TimeWindow(lo, hi + timedelta(microseconds=1))


def test_suppress_validation_boundaries():
    case = Case(rows=({"d": date(2026, 1, 1), "k": "U1", "c": "Cat 1",
                       "v": 1, "fk": "K1"},),
                dim_rows=DIMS, snapshot=False, fys=7, measures=dict(MEASURES))
    model = build_model(case)
    with pytest.raises(SemanticsError, match="positive integer"):
        compile_slice(model, ["n"], suppress=0)
    compile_slice(model, ["n"], suppress=1)     # 1 is a valid threshold


def test_ignored_entry_does_not_swallow_later_entries():
    """The miss rule skips-and-records; it must never stop the walk —
    entries after an ignored one still apply."""
    case = Case(rows=(), dim_rows=DIMS, snapshot=False, fys=7,
                measures=dict(MEASURES))
    model = build_model(case)
    from wh.metrics.context_ops import Context
    ctx = Context({"zzz__a": Eq("v"), "c__deep": Eq("v"), "c": Eq("Cat 1")})
    _time, attrs, applied, ignored = split_context(model, ctx)
    assert applied == ("c",)
    assert ignored == ("zzz__a", "c__deep")
    assert [(k, lhs) for k, lhs, _op in attrs] == [("c", "fact.c")]


# --- fixed-case differentials (deterministic, non-fuzz) ---

def test_fytd_lane_with_ratio_context_and_shared_dim():
    """One slice exercising every fytd-CTE internal at once: ratio
    num/den columns, multiple context predicates (local + shared dim,
    so the CTE needs the dim join), fiscal grain arithmetic, and a
    window hi that truncates the last period."""
    case = Case(
        rows=(
            {"d": date(2025, 7, 10), "k": "U1", "c": "Cat 1", "v": 80, "fk": "K1"},
            {"d": date(2025, 8, 10), "k": "U2", "c": "Cat 1", "v": 10, "fk": "K1"},
            {"d": date(2025, 8, 20), "k": "U3", "c": "Cat 3", "v": 90, "fk": "K2"},
            {"d": date(2025, 10, 5), "k": "U1", "c": "Cat 1", "v": 60, "fk": "K1"},
            {"d": date(2025, 11, 15), "k": "U4", "c": "Cat 2", "v": 70, "fk": "K1"},
            {"d": date(2025, 12, 30), "k": "U2", "c": "Cat 1", "v": 5, "fk": "K1"},
        ),
        dim_rows=DIMS, snapshot=False, fys=7, measures=dict(MEASURES),
    )
    args = SliceArgs(
        measures=("n", "pct"), by=(),
        ctx={"c": ("not", "Cat 2"), "facility.region": ("eq", "North")},
        time=(date(2025, 7, 1), date(2025, 12, 15)),
        grain="fy_quarter", compare=("fytd",),
    )
    expected = compare_oracle(case, args.measures, args.by, args.ctx,
                              args.time, args.grain, args.compare)
    assert_maps_equal(run_slice(case, args), expected)


def test_prior_at_day_grain_shifts_a_date_window():
    """Day/week grains shift by days, not months — the days-shift path
    of the window arithmetic."""
    case = Case(
        rows=tuple({"d": date(2025, 6, d), "k": "U1", "c": "Cat 1",
                    "v": 1, "fk": "K1"} for d in (8, 9, 9, 10, 11)),
        dim_rows=DIMS, snapshot=False, fys=1, measures=dict(MEASURES),
    )
    args = SliceArgs(measures=("n",), by=(), ctx={},
                     time=(date(2025, 6, 10), date(2025, 6, 11)),
                     grain="day", compare=("prior",))
    expected = compare_oracle(case, args.measures, args.by, args.ctx,
                              args.time, args.grain, args.compare)
    assert expected[(date(2025, 6, 10),)]["n_prior"] == 2   # the 06-09 rows
    assert_maps_equal(run_slice(case, args), expected)


def test_yoy_with_a_datetime_window_keeps_time_of_day():
    """The shifted window's datetime bounds keep their time-of-day; rows
    straddling the shifted hi instant discriminate the arithmetic."""
    case = Case(
        rows=(
            {"d": datetime(2025, 6, 15, 20, 0), "k": "U1", "c": "Cat 1", "v": 1, "fk": "K1"},
            {"d": datetime(2025, 6, 15, 22, 0), "k": "U2", "c": "Cat 1", "v": 1, "fk": "K1"},
            {"d": datetime(2025, 6, 16, 10, 0), "k": "U3", "c": "Cat 1", "v": 1, "fk": "K1"},
            {"d": datetime(2025, 6, 17, 10, 0), "k": "U4", "c": "Cat 1", "v": 1, "fk": "K1"},
            {"d": datetime(2026, 6, 10, 12, 0), "k": "U1", "c": "Cat 1", "v": 1, "fk": "K1"},
        ),
        dim_rows=DIMS, snapshot=False, fys=1, measures=dict(MEASURES),
    )
    args = SliceArgs(measures=("n",), by=(), ctx={},
                     time=(datetime(2026, 3, 1, 3, 0), datetime(2026, 6, 15, 21, 0)),
                     grain="month", compare=("yoy",))
    expected = compare_oracle(case, args.measures, args.by, args.ctx,
                              args.time, args.grain, args.compare)
    assert_maps_equal(run_slice(case, args), expected)


def test_suppress_guards_comparison_columns_by_their_own_lane():
    """A comparison column is suppressed by ITS lane's cell count, so a
    healthy base cell still reads NULL when its prior cell is tiny."""
    case = Case(
        rows=(
            {"d": date(2025, 5, 20), "k": "U1", "c": "Cat 1", "v": 1, "fk": "K1"},
            {"d": date(2025, 6, 5), "k": "U1", "c": "Cat 1", "v": 1, "fk": "K1"},
            {"d": date(2025, 6, 10), "k": "U2", "c": "Cat 1", "v": 1, "fk": "K1"},
            {"d": date(2025, 6, 20), "k": "U3", "c": "Cat 1", "v": 1, "fk": "K1"},
        ),
        dim_rows=DIMS, snapshot=False, fys=1, measures=dict(MEASURES),
    )
    args = SliceArgs(measures=("n",), by=(), ctx={}, time=None,
                     grain="month", compare=("prior",), suppress=2)
    expected = compare_oracle(case, ("n",), (), {}, None, "month", ("prior",))
    expected = apply_suppress(case, ("n",), (), {}, None, "month",
                              ("prior",), expected, 2)
    june = (date(2025, 6, 1),)
    assert expected[june]["n"] == 3
    assert expected[june]["n_prior"] is None     # May has 1 row < 2
    assert_maps_equal(run_slice(case, args), expected)


def test_complete_periods_drops_window_truncated_period_under_compare():
    """A period the window hi cuts mid-way is incomplete even when the
    DATA covers it — the completeness predicate must see the window."""
    case = Case(
        rows=(
            {"d": date(2025, 5, 10), "k": "U1", "c": "Cat 1", "v": 1, "fk": "K1"},
            {"d": date(2025, 5, 25), "k": "U2", "c": "Cat 1", "v": 1, "fk": "K1"},
            {"d": date(2025, 6, 5), "k": "U1", "c": "Cat 1", "v": 1, "fk": "K1"},
            {"d": date(2025, 6, 30), "k": "U2", "c": "Cat 1", "v": 1, "fk": "K1"},
        ),
        dim_rows=DIMS, snapshot=False, fys=1, measures=dict(MEASURES),
    )
    args = SliceArgs(measures=("n",), by=(), ctx={},
                     time=(date(2025, 5, 1), date(2025, 6, 15)),
                     grain="month", compare=("prior",), complete_periods=True)
    expected = compare_oracle(case, ("n",), (), {}, args.time, "month", ("prior",))
    expected = complete_filter(case, expected, "month", args.time)
    assert set(expected) == {(date(2025, 5, 1),)}   # June truncated at 06-15
    assert_maps_equal(run_slice(case, args), expected)


def test_values_scoped_by_shared_dim_context():
    """values() under a shared-dim context needs the dim join in its
    scan — North rows only, distinct, NULLs dropped, ordered."""
    case = Case(
        rows=(
            {"d": date(2026, 1, 1), "k": "U1", "c": "Cat 2", "v": 1, "fk": "K1"},
            {"d": date(2026, 1, 2), "k": "U2", "c": "Cat 1", "v": 1, "fk": "K1"},
            {"d": date(2026, 1, 3), "k": "U3", "c": "Cat 3", "v": 1, "fk": "K2"},
            {"d": date(2026, 1, 4), "k": "U4", "c": None, "v": 1, "fk": "K1"},
        ),
        dim_rows=DIMS, snapshot=False, fys=7, measures=dict(MEASURES),
    )
    con = duckdb.connect()
    seed(con, case)
    args = SliceArgs(measures=("n",), by=(),
                     ctx={"facility.region": ("eq", "North")},
                     time=None, grain=None)
    sql = compile_values(build_model(case), "c", ctx=to_wh_context(args))
    assert [r[0] for r in con.execute(sql).fetchall()] == ["Cat 1", "Cat 2"]


def test_lit_escapes_quotes_inside_the_nul_path():
    assert _lit("O'\x00B") == "('O''' || chr(0) || 'B')"


def test_prior_at_week_grain_truncates_the_shifted_window():
    """The shifted window's HI must shift too: a window ending mid-week
    truncates the prior week's comparison cell to the same weekday."""
    case = Case(
        rows=(
            {"d": date(2025, 6, 2), "k": "U1", "c": "Cat 1", "v": 1, "fk": "K1"},   # Mon W1
            {"d": date(2025, 6, 5), "k": "U2", "c": "Cat 1", "v": 1, "fk": "K1"},   # Thu W1
            {"d": date(2025, 6, 9), "k": "U3", "c": "Cat 1", "v": 1, "fk": "K1"},   # Mon W2
            {"d": date(2025, 6, 11), "k": "U4", "c": "Cat 1", "v": 1, "fk": "K1"},  # Wed W2
        ),
        dim_rows=DIMS, snapshot=False, fys=1, measures=dict(MEASURES),
    )
    args = SliceArgs(measures=("n",), by=(), ctx={},
                     time=(date(2025, 6, 9), date(2025, 6, 11)),   # Mon-Wed of W2
                     grain="week", compare=("prior",))
    expected = compare_oracle(case, args.measures, args.by, args.ctx,
                              args.time, args.grain, args.compare)
    # W1's comparison cell is Mon-Wed only: the Thu row must NOT count
    assert expected[(date(2025, 6, 9),)]["n_prior"] == 1
    assert_maps_equal(run_slice(case, args), expected)


def test_cadence_completeness_at_a_fiscal_grain():
    """Snapshot + weekly cadence + fy_quarter grain + July FY start: the
    cadence branch of the completeness predicate does fiscal arithmetic."""
    case = Case(
        rows=(
            {"d": date(2025, 7, 7), "k": "U1", "c": "Cat 1", "v": 1, "fk": "K1"},
            {"d": date(2025, 9, 29), "k": "U1", "c": "Cat 1", "v": 1, "fk": "K1"},
            {"d": date(2025, 10, 6), "k": "U1", "c": "Cat 1", "v": 1, "fk": "K1"},
        ),
        dim_rows=DIMS, snapshot=True, fys=7, cadence="weekly",
        measures={k: v for k, v in MEASURES.items() if k != "amt"},
    )
    args = SliceArgs(measures=("n",), by=(), ctx={}, time=None,
                     grain="fy_quarter", complete_periods=True)
    from oracle import slice_oracle
    expected = slice_oracle(case, ("n",), grain="fy_quarter")
    expected = complete_filter(case, expected, "fy_quarter", None)
    # Q1 (Jul-Sep) has its final expected census (09-29 within 7 days of
    # 09-30); Q2 has only its first week and must be dropped
    assert set(expected) == {(date(2025, 7, 1),)}
    assert_maps_equal(run_slice(case, args), expected)
