"""Behavioural fixtures with intent: results, never SQL text.

The seeded mirror is in conftest.con — June 2026 weekly snapshots with a
lagging clinic (C3 absent from the June-final 06-26 snapshot)."""

import pytest

from wh.metrics.compiler import compile_slice
from wh.metrics.context_ops import context


def run(con, compiled):
    rows = con.execute(compiled.sql).fetchall()
    cols = [d[0] for d in con.description]
    return [dict(zip(cols, r)) for r in rows]


def by_key(rows, *keys):
    return {tuple(r[k] for k in keys): r for r in rows}


def test_stock_rollup_is_last_snapshot_not_sum(con, defs):
    june = context(time=("2026-06-01", "2026-06-30"))
    rows = run(con, compile_slice(
        defs["waitlist"], ["patients_waiting"], grain="month", ctx=june
    ))
    assert rows == [{"period": rows[0]["period"], "patients_waiting": 4}]  # not 17


def test_median_is_at_end_of_month_never_over_mixed_rows(con, defs):
    june = context(time=("2026-06-01", "2026-06-30"))
    rows = run(con, compile_slice(defs["waitlist"], ["median_wait"], ctx=june))
    assert rows[0]["median_wait"] == 96.0        # median(121, 71, 421, 12)


def test_monthly_periods_each_get_their_own_asat(con, defs):
    rows = run(con, compile_slice(defs["waitlist"], ["patients_waiting"], grain="month"))
    assert [r["patients_waiting"] for r in rows] == [4, 4]   # June@06-26, July@07-10


def test_lagging_clinic_reads_as_absent_at_the_global_moment(con, defs):
    """C3's June feed stops at 06-19; the global June as-at is 06-26."""
    june = context(time=("2026-06-01", "2026-06-30"))
    per_region = by_key(
        run(con, compile_slice(
            defs["waitlist"], ["patients_waiting"], by=["facility.region"], ctx=june
        )),
        "facility.region",
    )
    assert per_region[("North",)]["patients_waiting"] == 3   # U1,U2,U3
    assert per_region[("South",)]["patients_waiting"] == 1   # U5 only; C3/U4 absent


def test_attribute_filter_shares_the_unfiltered_moment(con, defs):
    """Lane separation on the evaluation moment: filtering to the lagging
    region must NOT slide the as-at back to a snapshot where C3 was present."""
    june_south = context(time=("2026-06-01", "2026-06-30"), facility__region="South")
    rows = run(con, compile_slice(defs["waitlist"], ["patients_waiting"], ctx=june_south))
    assert rows[0]["patients_waiting"] == 1      # not 2 (U4+U5 at 06-19)


LANE_CASES = [
    (context(), ""),
    (context(facility__region="North"), "WHERE facility.region = 'North'"),
    (context(doctor__specialty=["ENT"]), "WHERE doctor.specialty IN ('ENT')"),
    (
        context(time=("2026-06-01", "2026-06-30")),
        "WHERE fact.removal_date BETWEEN DATE '2026-06-01' AND DATE '2026-06-30'",
    ),
    (
        context(
            facility__region="North",
            doctor__specialty=["ENT", "Ophthal"],
            time=("2026-06-01", "2026-07-31"),
        ),
        "WHERE fact.removal_date BETWEEN DATE '2026-06-01' AND DATE '2026-07-31'"
        " AND facility.region = 'North'"
        " AND doctor.specialty IN ('ENT', 'Ophthal')",
    ),
]


@pytest.mark.parametrize("ctx,hand_where", LANE_CASES)
def test_lane_isolation_property(con, defs, ctx, hand_where):
    """Layer vs hand-written over the same in-scope rows: no context can
    change the intrinsic predicate."""
    layer = run(con, compile_slice(defs["removals"], ["removals"], ctx=ctx))
    (hand,) = con.execute(f"""
        SELECT count(*) FILTER (WHERE removal_reason <> 'ADMIN')
        FROM main.waitlist_removals AS fact
        LEFT JOIN main.clinic_dim AS facility
               ON fact.clinic_code = facility.clinic_code
        LEFT JOIN main.doctor_dim AS doctor ON fact.doctor_id = doctor.doctor_id
        {hand_where}
    """).fetchone()
    assert layer[0]["removals"] == hand


def test_drill_consistency_region_totals_equal_district_sums(con, defs):
    june = context(time=("2026-06-01", "2026-06-30"))
    regions = by_key(
        run(con, compile_slice(defs["removals"], ["removals"], by=["facility.region"], ctx=june)),
        "facility.region",
    )
    districts = run(
        con, compile_slice(defs["removals"], ["removals"], by=["facility.district"], ctx=june)
    )
    to_region = {"Coastal": "North", "Inland": "North", "Seaside": "South", "Range": "South"}
    for region in ("North", "South"):
        district_sum = sum(
            r["removals"] for r in districts
            if to_region.get(r["facility.district"]) == region
        )
        assert regions.get((region,), {"removals": 0})["removals"] == district_sum


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
