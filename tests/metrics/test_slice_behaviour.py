"""Behavioural fixtures with intent: results, never SQL text.

The seeded mirror is in conftest.con — June 2026 weekly snapshots with a
lagging clinic (C3 absent from the June-final 06-26 snapshot)."""

from wh.metrics.compiler import compile_slice
from wh.metrics.context_ops import context


def run(con, compiled):
    rows = con.execute(compiled.sql).fetchall()
    cols = [d[0] for d in con.description]
    return [dict(zip(cols, r)) for r in rows]


def by_key(rows, *keys):
    return {tuple(r[k] for k in keys): r for r in rows}


def test_ratio_at_unequal_group_sizes(con, defs):
    """The avg-of-ratios trap: overall != mean of group ratios."""
    ctx = context(time=("2026-06-26", "2026-06-26"))     # one snapshot: U1,U2,U3,U5
    overall = run(con, compile_slice(defs["waitlist"], ["pct_over_target"], ctx=ctx))
    assert overall[0]["pct_over_target"] == 0.75          # 3 of 4 over target
    per_region = by_key(
        run(con, compile_slice(
            defs["waitlist"], ["pct_over_target"], by=["facility.region"], ctx=ctx
        )),
        "facility.region",
    )
    assert per_region[("North",)]["pct_over_target"] == 1.0   # U1,U2,U3
    assert per_region[("South",)]["pct_over_target"] == 0.0   # U5 under target
