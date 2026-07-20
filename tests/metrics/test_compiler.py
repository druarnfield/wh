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
    facility: clinic_code
    urgency: urgency_code
  measures:
    n:
      expr: count(*)
      description: "Events"
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
        WHERE fact.urgency_code <> 'Cat 3'
        GROUP BY ALL
    """)


def test_time_context_is_a_between_on_the_time_column(con, defs):
    c = compile_slice(
        defs["removals"], ["removals"], ctx=context(time=("2025-07-01", "2026-06-30"))
    )
    assert_sql_equiv(con, c.sql, """
        SELECT count(*) FILTER (WHERE removal_reason <> 'ADMIN') AS removals
        FROM main.waitlist_removals AS fact
        WHERE fact.removal_date BETWEEN DATE '2025-07-01' AND DATE '2026-06-30'
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


def test_unresolved_context_is_refused(defs):
    class W:
        value = ["ENT"]

    with pytest.raises(SemanticsError, match="resolve"):
        compile_slice(defs["removals"], ["removals"], ctx=context(doctor__specialty=W()))
