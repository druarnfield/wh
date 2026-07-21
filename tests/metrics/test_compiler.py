import pytest

from wh.errors import SemanticsError
from wh.metrics.compiler import compile_slice
from wh.metrics.context_ops import EMPTY, context, not_

from treecheck import assert_sql_equiv

EVENTS_YAML = """\
events:
  fact: main.events
  time:
    column: event_date
  dimensions:
    facility: {shared: clinic_code}
    urgency: urgency_code
  measures:
    n:
      expr: count(*)
      description: "Events"
    pct_urgent:
      ratio:
        num: count(*) FILTER (WHERE urgency_code = 'Cat 1')
        den: count(*)
      description: "% urgent"
"""


def test_lanes_and_pruning(con, defs):
    c = compile_slice(
        defs["removals"], ["removals"], by=["facility.region"],
        ctx=context(facility__region="North"), grain="month",
    )
    assert_sql_equiv(con, c.sql, """
        SELECT CAST(date_trunc('month', fact.removal_date) AS DATE) AS period,
               facility.region AS "facility.region",
               count(*) FILTER (WHERE removal_reason <> 'ADMIN') AS removals
        FROM main.waitlist_removals AS fact
        LEFT JOIN main.clinic_dim AS facility
               ON fact.clinic_code = facility.clinic_code
        WHERE facility.region = 'North'
        GROUP BY ALL
        ORDER BY period
    """)


def test_no_join_when_dim_unreferenced(con, defs):
    c = compile_slice(defs["removals"], ["removals"])
    assert "JOIN" not in c.sql.upper()
    assert_sql_equiv(con, c.sql, """
        SELECT count(*) FILTER (WHERE removal_reason <> 'ADMIN') AS removals
        FROM main.waitlist_removals AS fact
        GROUP BY ALL
    """)


def test_context_alone_triggers_join(con, defs):
    c = compile_slice(
        defs["removals"], ["removals"], ctx=context(doctor__specialty=["ENT", "Ophthal"])
    )
    assert_sql_equiv(con, c.sql, """
        SELECT count(*) FILTER (WHERE removal_reason <> 'ADMIN') AS removals
        FROM main.waitlist_removals AS fact
        LEFT JOIN main.doctor_dim AS doctor ON fact.doctor_id = doctor.doctor_id
        WHERE doctor.specialty IN ('ENT', 'Ophthal')
        GROUP BY ALL
    """)


def test_local_dim_needs_no_join(con, make_defs, design_yaml):
    defs = make_defs(design_yaml["dims"], EVENTS_YAML)
    c = compile_slice(
        defs["events"], ["n"], by=["urgency"], ctx=context(urgency=not_("Cat 3"))
    )
    assert_sql_equiv(con, c.sql, """
        SELECT fact.urgency_code AS urgency, count(*) AS n
        FROM main.events AS fact
        WHERE fact.urgency_code IS DISTINCT FROM 'Cat 3'
        GROUP BY ALL
    """)


def test_time_context_is_day_inclusive_on_both_ends(con, defs):
    c = compile_slice(
        defs["removals"], ["removals"], ctx=context(time=("2025-07-01", "2026-06-30"))
    )
    assert_sql_equiv(con, c.sql, """
        SELECT count(*) FILTER (WHERE removal_reason <> 'ADMIN') AS removals
        FROM main.waitlist_removals AS fact
        WHERE fact.removal_date >= DATE '2025-07-01'
          AND fact.removal_date < DATE '2026-06-30' + INTERVAL 1 DAY
        GROUP BY ALL
    """)


def test_miss_rule_applied_and_ignored(defs, make_defs, design_yaml):
    evdefs = make_defs(design_yaml["dims"], EVENTS_YAML)
    c = compile_slice(
        evdefs["events"], ["n"],
        ctx=context(facility__region="North", doctor__specialty="ENT", urgency="Cat 1"),
    )
    assert c.applied == ("facility__region", "urgency")
    assert c.ignored == ("doctor__specialty",)     # events has no doctor dim


def test_ratio_division_outermost_components_beneath(con, make_defs, design_yaml):
    defs = make_defs(design_yaml["dims"], EVENTS_YAML)
    c = compile_slice(
        defs["events"], ["n", "pct_urgent"], by=["facility.region"], grain="month"
    )
    assert_sql_equiv(con, c.sql, """
        SELECT period,
               "facility.region",
               n,
               CAST(__pct_urgent_num AS DOUBLE) / NULLIF(__pct_urgent_den, 0)
                   AS pct_urgent
        FROM (
            SELECT CAST(date_trunc('month', fact.event_date) AS DATE) AS period,
                   facility.region AS "facility.region",
                   count(*) AS n,
                   count(*) FILTER (WHERE urgency_code = 'Cat 1') AS __pct_urgent_num,
                   count(*) AS __pct_urgent_den
            FROM main.events AS fact
            LEFT JOIN main.clinic_dim AS facility
                   ON fact.clinic_code = facility.clinic_code
            GROUP BY ALL
        )
        ORDER BY period
    """)


def test_no_wrapper_without_ratio_measures(defs):
    c = compile_slice(defs["removals"], ["removals"], grain="month")
    assert c.sql.count("SELECT") == 1


def test_snapshot_asat_join_shape(con, defs):
    c = compile_slice(
        defs["waitlist"], ["patients_waiting"], grain="month",
        ctx=context(time=("2026-06-01", "2026-06-30")),
    )
    assert_sql_equiv(con, c.sql, """
        SELECT CAST(date_trunc('month', fact.snapshot_date) AS DATE) AS period,
               count(DISTINCT ur) AS patients_waiting
        FROM main.waitlist AS fact
        JOIN (
            SELECT CAST(date_trunc('month', snapshot_date) AS DATE) AS __period,
                   max(snapshot_date) AS __as_at
            FROM main.waitlist
            WHERE snapshot_date >= DATE '2026-06-01'
              AND snapshot_date < DATE '2026-06-30' + INTERVAL 1 DAY
            GROUP BY 1
        ) AS __asat
          ON CAST(date_trunc('month', fact.snapshot_date) AS DATE) = __asat.__period
         AND fact.snapshot_date = __asat.__as_at
        WHERE fact.snapshot_date >= DATE '2026-06-01'
          AND fact.snapshot_date < DATE '2026-06-30' + INTERVAL 1 DAY
        GROUP BY ALL
        ORDER BY period
    """)


def test_snapshot_asat_without_grain_is_one_global_moment(con, defs):
    c = compile_slice(defs["waitlist"], ["patients_waiting"])
    assert_sql_equiv(con, c.sql, """
        SELECT count(DISTINCT ur) AS patients_waiting
        FROM main.waitlist AS fact
        JOIN (
            SELECT max(snapshot_date) AS __as_at FROM main.waitlist
        ) AS __asat ON fact.snapshot_date = __asat.__as_at
        GROUP BY ALL
    """)


def test_unknown_measure_names_available(defs):
    with pytest.raises(SemanticsError, match="removals"):
        compile_slice(defs["removals"], ["nope"])


def test_unknown_attribute_on_declared_dim_errors(defs):
    with pytest.raises(SemanticsError, match="region"):
        compile_slice(defs["removals"], ["removals"], ctx=context(facility__galaxy="X"))
    with pytest.raises(SemanticsError, match="galaxy"):
        compile_slice(defs["removals"], ["removals"], by=["facility.galaxy"])


def test_shared_dim_context_without_attribute_errors(defs):
    with pytest.raises(SemanticsError, match="facility__"):
        compile_slice(defs["removals"], ["removals"], ctx=context(facility="C1"))


def test_attr_syntax_on_a_local_dim_is_ignored_not_an_error(defs):
    """Miss rule symmetry: 'urgency__band' can't apply to waitlist (local
    dim, no attributes) — skipped and recorded, same as on any other model,
    so one context works across a model set. strict_context still catches it."""
    c = compile_slice(defs["waitlist"], ["long_waiters"], ctx=context(urgency__band="x"))
    assert c.ignored == ("urgency__band",)


def test_time_in_by_points_at_grain(defs):
    with pytest.raises(SemanticsError, match="grain"):
        compile_slice(defs["removals"], ["removals"], by=["time"])


def test_empty_measures_is_an_error(defs):
    with pytest.raises(SemanticsError, match="measure"):
        compile_slice(defs["removals"], [])


def test_unresolved_context_is_refused(defs):
    class W:
        value = ["ENT"]

    with pytest.raises(SemanticsError, match="resolve"):
        compile_slice(defs["removals"], ["removals"], ctx=context(doctor__specialty=W()))


def test_duplicate_output_columns_error(defs):
    with pytest.raises(SemanticsError, match="produced twice"):
        compile_slice(defs["removals"], ["removals", "removals"])
    with pytest.raises(SemanticsError, match="produced twice"):
        compile_slice(
            defs["removals"], ["removals"],
            by=["facility.region", "facility.region"],
        )


def test_by_entry_colliding_with_comparison_column_errors(make_defs):
    defs = make_defs("""\
events:
  fact: main.events
  time: {column: d}
  dimensions:
    n_prior: category
  measures:
    n: {description: n, expr: "count(*)"}
""")
    with pytest.raises(SemanticsError, match="n_prior"):
        compile_slice(
            defs["events"], ["n"], by=["n_prior"], grain="month",
            compare=["prior"],
        )
