"""compare= — shifted-CTE self-joins, never lag; own as-at per period."""

from datetime import date

import pytest

from wh.errors import SemanticsError
from wh.metrics.compiler import compile_slice
from wh.metrics.context_ops import context
from wh.metrics.timegrain import fy_start

from treecheck import assert_sql_equiv


def run(con, compiled):
    rows = con.execute(compiled.sql).fetchall()
    cols = [d[0] for d in con.description]
    return [dict(zip(cols, r)) for r in rows]


# --- Task 1: validity + helpers ---


def test_fy_start():
    assert fy_start(date(2026, 3, 10), 7) == date(2025, 7, 1)
    assert fy_start(date(2026, 8, 1), 7) == date(2026, 7, 1)
    assert fy_start(date(2026, 3, 10), 1) == date(2026, 1, 1)


def test_compare_needs_grain(defs):
    with pytest.raises(SemanticsError, match="grain"):
        compile_slice(defs["removals"], ["removals"], compare=["yoy"])


def test_unknown_compare_lists_the_vocabulary(defs):
    with pytest.raises(SemanticsError, match="fytd"):
        compile_slice(defs["removals"], ["removals"], grain="month", compare=["wow"])


def test_fytd_on_a_stock_measure_errors_naming_the_fix(defs):
    with pytest.raises(SemanticsError, match="event-grain"):
        compile_slice(
            defs["waitlist"], ["patients_waiting"], grain="month", compare=["fytd"]
        )


def test_time_agg_none_refuses_any_compare(defs):
    with pytest.raises(SemanticsError, match="median_wait"):
        compile_slice(defs["waitlist"], ["median_wait"], grain="month", compare=["prior"])


def test_yoy_at_week_grain_errors(defs):
    with pytest.raises(SemanticsError, match="week"):
        compile_slice(defs["removals"], ["removals"], grain="week", compare=["yoy"])


def test_comparison_suffix_collision_errors(make_defs, design_yaml):
    yaml = design_yaml["removals"].replace(
        "  measures:\n",
        "  measures:\n    removals_prior:\n      expr: count(*)\n"
        "      description: unlucky name\n",
    )
    defs = make_defs(design_yaml["dims"], yaml)
    with pytest.raises(SemanticsError, match="removals_prior"):
        compile_slice(defs["removals"], ["removals"], grain="month", compare=["prior"])


# --- Task 2: prior/yoy compilation ---


def test_compare_cte_shape_and_scan_widening(con, defs):
    c = compile_slice(
        defs["removals"], ["removals"], by=["facility.region"], grain="month",
        ctx=context(time=("2026-06-01", "2026-07-31")), compare=["yoy"],
    )
    assert c.scan_lo == {"yoy": date(2025, 6, 1)}
    assert_sql_equiv(con, c.sql, """
        WITH __out AS (
            SELECT CAST(date_trunc('month', fact.removal_date) AS DATE) AS period,
                   facility.region AS "facility.region",
                   count(*) FILTER (WHERE removal_reason <> 'ADMIN') AS removals
            FROM main.waitlist_removals AS fact
            LEFT JOIN main.clinic_dim AS facility
                   ON fact.clinic_code = facility.clinic_code
            WHERE fact.removal_date >= DATE '2026-06-01'
              AND fact.removal_date < DATE '2026-07-31' + INTERVAL 1 DAY
            GROUP BY ALL
        ), __cmp_yoy AS (
            SELECT CAST(date_trunc('month', fact.removal_date) AS DATE) AS period,
                   facility.region AS "facility.region",
                   count(*) FILTER (WHERE removal_reason <> 'ADMIN') AS removals
            FROM main.waitlist_removals AS fact
            LEFT JOIN main.clinic_dim AS facility
                   ON fact.clinic_code = facility.clinic_code
            WHERE fact.removal_date >= DATE '2025-06-01'
              AND fact.removal_date < DATE '2025-07-31' + INTERVAL 1 DAY
            GROUP BY ALL
        )
        SELECT b.period,
               b."facility.region",
               b.removals,
               __cmp_yoy.removals AS removals_yoy
        FROM __out AS b
        LEFT JOIN __cmp_yoy
               ON __cmp_yoy.period = b.period - INTERVAL 12 MONTH
              AND __cmp_yoy."facility.region" IS NOT DISTINCT FROM b."facility.region"
        ORDER BY period
    """)


def test_prior_aligns_by_group_and_gaps_stay_null(con, defs):
    """South has no June rows: its July prior must be NULL — a lag() would
    slip North's June value into the gap."""
    rows = run(con, compile_slice(
        defs["removals"], ["removals"], by=["facility.region"],
        grain="month", compare=["prior"],
    ))
    got = {(r["period"], r["facility.region"]): r for r in rows}
    july = date(2026, 7, 1)
    june = date(2026, 6, 1)
    assert got[(july, "North")]["removals_prior"] == 2      # June North
    assert got[(july, "South")]["removals_prior"] is None   # gap, not slipped
    assert got[(june, "North")]["removals_prior"] is None   # off the data start


def test_yoy_off_data_start_is_null_not_a_crash(con, defs):
    rows = run(con, compile_slice(
        defs["removals"], ["removals"], grain="month", compare=["yoy"],
    ))
    assert all(r["removals_yoy"] is None for r in rows)


def test_ratio_measures_compare_too(con, defs):
    rows = run(con, compile_slice(
        defs["waitlist"], ["pct_over_target"], grain="month", compare=["prior"],
    ))
    got = {r["period"]: r for r in rows}
    june = got[date(2026, 6, 1)]["pct_over_target"]
    assert got[date(2026, 7, 1)]["pct_over_target_prior"] == june == 0.75


# --- Task 4: fytd from base rows ---


def test_fytd_resets_at_the_fiscal_boundary(con, defs):
    """June 2026 is FY2025-26; July 2026 starts FY2026-27. July's fytd must
    be July-only (2), not cumulative-across-FYs (4)."""
    rows = run(con, compile_slice(
        defs["removals"], ["removals"], grain="month", compare=["fytd"],
    ))
    got = {r["period"]: r for r in rows}
    assert got[date(2026, 6, 1)]["removals_fytd"] == 2
    assert got[date(2026, 7, 1)]["removals"] == 2
    assert got[date(2026, 7, 1)]["removals_fytd"] == 2       # reset, not 4


def test_fytd_distinct_counts_come_from_base_not_window_sums(con, make_defs):
    """A patient attending in two months counts once in fytd — summing the
    monthly distinct counts would double them."""
    con.execute(
        "CREATE TABLE main.att AS FROM (VALUES "
        "(DATE '2025-08-03','A'), (DATE '2025-08-05','B'), (DATE '2025-09-14','A')"
        ") t(att_date, ur)"
    )
    m = make_defs(
        "att:\n  fact: main.att\n  time:\n    column: att_date\n"
        "  measures:\n    patients:\n      expr: count(DISTINCT ur)\n"
        "      description: d\n"
    )["att"]
    rows = run(con, compile_slice(m, ["patients"], grain="month", compare=["fytd"]))
    got = {r["period"]: r for r in rows}
    assert got[date(2025, 9, 1)]["patients_fytd"] == 2       # A once, not 2+1=3


def test_fytd_respects_the_context_upper_bound(con, defs):
    """Mid-period truncation carries into fytd: a window ending 07-05 must
    exclude the 07-08 removal from July's fytd."""
    rows = run(con, compile_slice(
        defs["removals"], ["removals"], grain="month", compare=["fytd"],
        ctx=context(time=("2026-06-01", "2026-07-05")),
    ))
    got = {r["period"]: r for r in rows}
    assert got[date(2026, 7, 1)]["removals_fytd"] == 1       # 07-01 only
    assert rows and compile_slice(
        defs["removals"], ["removals"], grain="month", compare=["fytd"],
        ctx=context(time=("2026-06-01", "2026-07-05")),
    ).scan_lo == {"fytd": date(2025, 7, 1)}                  # widened to FY start


# --- Task 5: complete_periods ---


def test_complete_periods_drops_the_trailing_partial_month(con, defs):
    rows = run(con, compile_slice(
        defs["removals"], ["removals"], grain="month", complete_periods=True,
    ))
    assert [r["period"] for r in rows] == [date(2026, 6, 1)]   # July (max 07-08) dropped


def test_complete_periods_needs_grain(defs):
    with pytest.raises(SemanticsError, match="grain"):
        compile_slice(defs["removals"], ["removals"], complete_periods=True)


def test_snapshot_cadence_completeness_beats_the_max_date_heuristic(con, make_defs):
    """Weekly snapshots, last on the 28th of a 31-day month: the final
    EXPECTED snapshot landed (complete under cadence), while the max-date
    rule would call the month incomplete."""
    con.execute(
        "CREATE TABLE main.wl2 AS FROM (VALUES "
        "(DATE '2026-07-07','A'), (DATE '2026-07-14','A'), "
        "(DATE '2026-07-21','A'), (DATE '2026-07-28','A')) t(snap_date, ur)"
    )
    base = (
        "wl2:\n  fact: main.wl2\n  time:\n    column: snap_date\n{cadence}"
        "  snapshot: true\n"
        "  measures:\n    n:\n      expr: count(DISTINCT ur)\n      description: d\n"
    )
    with_cadence = make_defs(base.format(cadence="    cadence: weekly\n"))["wl2"]
    # yaml indent: cadence belongs under time:
    rows = run(con, compile_slice(with_cadence, ["n"], grain="month", complete_periods=True))
    assert [r["period"] for r in rows] == [date(2026, 7, 1)]   # complete

    without = make_defs(base.format(cadence=""))["wl2"]
    rows = run(con, compile_slice(without, ["n"], grain="month", complete_periods=True))
    assert rows == []                                          # max-date rule: dropped


def test_invalid_cadence_is_a_load_error(make_defs):
    with pytest.raises(SemanticsError, match="cadence"):
        make_defs(
            "m:\n  fact: t\n  time:\n    column: c\n    cadence: fortnightly\n"
            "  measures:\n    n:\n      expr: count(*)\n      description: d\n"
        )


# --- Task 3: snapshot comparisons carry their own as-at ---


def test_snapshot_prior_uses_the_prior_periods_own_asat(con, defs):
    rows = run(con, compile_slice(
        defs["waitlist"], ["patients_waiting"], by=["facility.region"],
        grain="month", compare=["prior"],
    ))
    got = {(r["period"], r["facility.region"]): r for r in rows}
    july = date(2026, 7, 1)
    # June evaluated at ITS as-at (06-26): North 3, South 1 (C3 lagging absent)
    assert got[(july, "North")]["patients_waiting_prior"] == 3
    assert got[(july, "South")]["patients_waiting_prior"] == 1
