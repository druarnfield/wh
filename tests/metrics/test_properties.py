"""Metamorphic relations between the compiler's own answers. These hold
for ANY correct implementation of the documented semantics — they defend
against spec-level misreadings that differential testing cannot see."""

import duckdb
import pytest
from datetime import date, datetime, timedelta
from hypothesis import assume, given

from wh.metrics.compiler import compile_slice

from oracle import result_map, prev_period
from strategies import build_model, scenarios, seed, to_wh_context, Case, SliceArgs


def run(case, args):
    con = duckdb.connect()
    seed(con, case)
    compiled = compile_slice(
        build_model(case), list(args.measures), by=list(args.by),
        ctx=to_wh_context(args), grain=args.grain,
        compare=list(args.compare), suppress=args.suppress,
    )
    return result_map(
        con.execute(compiled.sql), args.by, args.measures, args.grain,
        compare=args.compare,
    )


ADDITIVE = ("n", "amt", "big")     # sum-family measures partition cleanly


@pytest.mark.fuzz
@given(sc=scenarios())
def test_by_partitions_the_total(sc):
    """Sum of by-group cells == the unfiltered total for additive
    measures. Standing tripwire for join fan-out and lane leakage."""
    case, args = sc
    measures = tuple(m for m in args.measures if m in ADDITIVE)
    assume(measures)
    args_by = SliceArgs(measures=measures, by=("facility.region",),
                        ctx=args.ctx, time=args.time, grain=args.grain)
    args_tot = SliceArgs(measures=measures, by=(), ctx=args.ctx,
                         time=args.time, grain=args.grain)
    parts, total = run(case, args_by), run(case, args_tot)
    for m in measures:
        by_period = {}
        for gk, vals in parts.items():
            p = gk[0] if args.grain else ()
            if vals[m] is not None:
                by_period[p] = by_period.get(p, 0) + vals[m]
        for gk, vals in total.items():
            p = gk[0] if args.grain else ()
            assert by_period.get(p, 0) == (vals[m] or 0), \
                f"{m} at {p}: groups sum to {by_period.get(p)} != {vals[m]}"


@pytest.mark.fuzz
@given(sc=scenarios(event_only=True, require_ctx=True))
def test_context_filter_equals_prefiltered_data(sc):
    """Filtering via context == deleting the non-matching rows first.
    Catches any filter leaking into the wrong lane."""
    from oracle import passes, _attr
    case, args = sc
    assume(args.ctx and not case.snapshot)   # as-at moves under row deletion
    kept = tuple(r for r in case.rows
                 if all(passes(_attr(case, r, k), op)
                        for k, op in args.ctx.items()))
    pre = Case(rows=kept, dim_rows=case.dim_rows, snapshot=False,
               fys=case.fys, measures=case.measures)
    args_nofilter = SliceArgs(measures=args.measures, by=args.by, ctx={},
                              time=args.time, grain=args.grain)
    assert run(case, args) == run(pre, args_nofilter)


@pytest.mark.fuzz
@given(sc=scenarios())
def test_adding_a_filter_never_increases_a_count(sc):
    case, args = sc
    measures = tuple(m for m in args.measures if m in ("n", "big", "ents"))
    assume(measures and "c" not in args.ctx)
    a = SliceArgs(measures=measures, by=args.by, ctx=args.ctx,
                  time=args.time, grain=args.grain)
    b = SliceArgs(measures=measures, by=args.by,
                  ctx={**args.ctx, "c": ("eq", "Cat 1")},
                  time=args.time, grain=args.grain)
    loose, tight = run(case, a), run(case, b)
    assert set(tight) <= set(loose)
    for gk, vals in tight.items():
        for m in measures:
            assert vals[m] <= loose[gk][m]


@pytest.mark.fuzz
@given(sc=scenarios(event_only=True, require_window="dates"))
def test_splitting_the_window_partitions_counts(sc):
    """[a, c] == [a, b] + [b+1, c] for sum-family measures. The standing
    tripwire for every inclusive/exclusive bound bug."""
    case, args = sc
    assume(args.time is not None and not case.snapshot)
    lo, hi = args.time
    assume(not isinstance(lo, datetime))
    assume((hi - lo).days >= 2)
    mid = lo + timedelta(days=(hi - lo).days // 2)
    measures = tuple(m for m in args.measures if m in ADDITIVE)
    assume(measures)
    whole = run(case, SliceArgs(measures=measures, by=args.by, ctx=args.ctx,
                                time=(lo, hi), grain=None))
    left = run(case, SliceArgs(measures=measures, by=args.by, ctx=args.ctx,
                               time=(lo, mid), grain=None))
    right = run(case, SliceArgs(measures=measures, by=args.by, ctx=args.ctx,
                                time=(mid + timedelta(days=1), hi), grain=None))
    for gk in set(whole) | set(left) | set(right):
        for m in measures:
            def val(r):
                return (r.get(gk) or {}).get(m) or 0
            assert val(left) + val(right) == val(whole), (gk, m)


@pytest.mark.fuzz
@given(sc=scenarios(with_compare=True))
def test_prior_equals_the_direct_value_of_the_previous_period(sc):
    """Spec-consistency: n_prior at P == n at prev(P) whenever the window
    is unbounded — no truncation, so the values must agree exactly."""
    case, args = sc
    assume(args.compare and "fytd" not in args.compare)
    args = SliceArgs(measures=args.measures, by=args.by, ctx=args.ctx,
                     time=None, grain=args.grain, compare=args.compare)
    res = run(case, args)
    for gk, vals in res.items():
        for cmp in args.compare:
            pk = (prev_period(gk[0], cmp, args.grain),) + gk[1:]
            for m in args.measures:
                direct = (res.get(pk) or {}).get(m)
                assert vals[f"{m}_{cmp}"] == direct, (gk, m, cmp)
