"""The design's lane promises, asserted on parse trees with canary literals.

Canary values substituted into context entries must surface ONLY in WHERE
clauses (the extrinsic lane) — never in select lists, measure FILTERs,
joins, or GROUP BY. Attribute canaries must additionally never enter the
__asat subquery (snapshot models); time canaries may.
"""

from datetime import date

import pytest

from wh.metrics.compiler import compile_slice
from wh.metrics.context_ops import context

from treecheck import canary_counts

ATTR_CANARY = "__CANARY_ATTR__"
TIME_CANARY = "1893-01-07"


def canary_ctx():
    return context(
        facility__region=ATTR_CANARY,
        doctor__specialty=[ATTR_CANARY, "other"],
        time=(date(1893, 1, 7), date(1893, 2, 1)),
    )


CASES = [
    ("removals", ["removals"], [], None, ()),                   # intrinsic where
    ("removals", ["removals"], ["facility.region"], "month", ()),
    ("waitlist", ["patients_waiting", "long_waiters"], [], None, ()),
    ("waitlist", ["long_waiters", "median_wait"], ["doctor.specialty"], "fy", ()),
    ("waitlist", ["pct_over_target"], ["facility.district"], "month", ()),  # ratio
    ("removals", ["removals"], ["facility.region"], "month", ("prior", "yoy")),
    ("waitlist", ["patients_waiting"], ["facility.region"], "month", ("prior",)),
]


@pytest.mark.parametrize("model,measures,by,grain,compare", CASES)
def test_attribute_canaries_only_in_the_extrinsic_lane(
    con, defs, model, measures, by, grain, compare
):
    c = compile_slice(
        defs[model], measures, by=by, ctx=canary_ctx(), grain=grain, compare=compare
    )
    in_where, in_asat, total = canary_counts(con, c.sql, ATTR_CANARY)
    assert total > 0, "canary context was not compiled at all"
    assert in_asat == 0, "attribute predicate leaked into the as-at subquery"
    assert in_where == total, "context value appeared outside the outer WHERE"
    if compare:
        # the context must FOLLOW the comparison rows: every CTE carries it
        base = compile_slice(defs[model], measures, by=by, ctx=canary_ctx(), grain=grain)
        _, _, base_total = canary_counts(con, base.sql, ATTR_CANARY)
        assert total == base_total * (1 + len(compare)), (
            "a comparison CTE dropped the attribute context"
        )


@pytest.mark.parametrize("model,measures,by,grain,compare", CASES)
def test_time_canaries_only_in_where_lanes(
    con, defs, model, measures, by, grain, compare
):
    c = compile_slice(
        defs[model], measures, by=by, ctx=canary_ctx(), grain=grain, compare=compare
    )
    in_where, in_asat, total = canary_counts(con, c.sql, TIME_CANARY)
    assert total > 0
    assert in_where + in_asat == total, "time value appeared outside WHERE lanes"
    if defs[model].snapshot:
        assert in_asat > 0, "as-at subquery lost its time truncation"
