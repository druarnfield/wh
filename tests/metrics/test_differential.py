"""Differential: compiled SQL vs the naive oracle, cell-exact, over
randomized cases. The meta-test proves the harness detects a planted
off-by-one before any green is trusted."""

import duckdb
import pytest
from hypothesis import given, note

from wh.metrics.compiler import compile_slice

from oracle import (apply_suppress, assert_maps_equal, compare_oracle,
                    complete_filter, result_map, slice_oracle)
from strategies import build_model, scenarios, seed, to_wh_context


def run_both(case, args):
    con = duckdb.connect()
    seed(con, case)
    model = build_model(case)
    compiled = compile_slice(
        model, list(args.measures), by=list(args.by),
        ctx=to_wh_context(args), grain=args.grain,
        compare=list(args.compare),
        complete_periods=args.complete_periods, suppress=args.suppress,
    )
    note(compiled.sql)
    actual = result_map(
        con.execute(compiled.sql), args.by, args.measures, args.grain,
        compare=args.compare,
    )
    return actual, compiled


@pytest.mark.fuzz
@given(sc=scenarios())
def test_plain_slices_match_the_oracle(sc):
    case, args = sc
    actual, _ = run_both(case, args)
    expected = slice_oracle(case, args.measures, by=args.by, ctx=args.ctx,
                            win=args.time, grain=args.grain)
    assert_maps_equal(actual, expected)


@pytest.mark.fuzz
@given(sc=scenarios(with_compare=True))
def test_compare_slices_match_the_oracle(sc):
    case, args = sc
    actual, _ = run_both(case, args)
    expected = compare_oracle(case, args.measures, args.by, args.ctx,
                              args.time, args.grain, args.compare)
    assert_maps_equal(actual, expected)


@pytest.mark.fuzz
@given(sc=scenarios(with_compare=True, with_suppress=True, with_complete=True))
def test_suppress_and_complete_periods_match_the_oracle(sc):
    case, args = sc
    actual, _ = run_both(case, args)
    expected = compare_oracle(case, args.measures, args.by, args.ctx,
                              args.time, args.grain, args.compare)
    if args.complete_periods:
        expected = complete_filter(case, expected, args.grain, args.time)
    if args.suppress is not None:
        expected = apply_suppress(case, args.measures, args.by, args.ctx,
                                  args.time, args.grain, args.compare,
                                  expected, args.suppress)
    assert_maps_equal(actual, expected)


def test_the_harness_bites():
    """Plant the classic bound bug (< widened to <=) in emitted SQL and
    prove the comparator catches it — otherwise green is vacuous."""
    from datetime import date
    from strategies import Case, MEASURES, SliceArgs

    case = Case(
        rows=(
            {"d": date(2026, 6, 30), "k": "U1", "c": "Cat 1", "v": 1, "fk": "K1"},
            {"d": date(2026, 7, 1), "k": "U2", "c": "Cat 1", "v": 1, "fk": "K1"},
        ),
        dim_rows=(("K1", "North"), ("K2", "South"), ("K3", "North")),
        snapshot=False, fys=7, measures=dict(MEASURES),
    )
    args = SliceArgs(measures=("n",), by=(), ctx={},
                     time=(date(2026, 6, 1), date(2026, 6, 30)), grain=None)
    con = duckdb.connect()
    seed(con, case)
    compiled = compile_slice(build_model(case), ["n"], ctx=to_wh_context(args))
    corrupted = compiled.sql.replace("< DATE", "<= DATE")
    assert corrupted != compiled.sql
    actual = result_map(con.execute(corrupted), (), ("n",), None)
    expected = slice_oracle(case, ("n",), win=args.time)
    with pytest.raises(AssertionError):
        assert_maps_equal(actual, expected)
