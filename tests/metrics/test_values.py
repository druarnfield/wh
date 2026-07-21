"""Possible values: cheap from the dim table, associative under a context."""

import pytest

from wh.errors import SemanticsError
from wh.metrics.compiler import compile_values
from wh.metrics.context_ops import context

from treecheck import assert_sql_equiv


def test_unscoped_shared_dim_reads_the_dim_table_only(con, defs):
    sql = compile_values(defs["waitlist"], "facility.region")
    assert "waitlist" not in sql                     # no fact scan — cheap
    assert_sql_equiv(con, sql, """
        SELECT DISTINCT region AS value
        FROM main.clinic_dim
        WHERE region IS NOT NULL
        ORDER BY 1
    """)


def test_scoped_values_go_through_the_lanes(con, defs):
    sql = compile_values(
        defs["waitlist"], "facility.clinic",
        ctx=context(facility__region="North", time=("2026-06-01", "2026-06-30")),
    )
    assert_sql_equiv(con, sql, """
        SELECT DISTINCT facility.clinic_name AS value
        FROM main.waitlist AS fact
        LEFT JOIN main.clinic_dim AS facility
               ON fact.clinic_code = facility.clinic_code
        WHERE fact.snapshot_date >= DATE '2026-06-01'
          AND fact.snapshot_date < DATE '2026-07-01'
          AND facility.region = 'North'
          AND facility.clinic_name IS NOT NULL
        ORDER BY 1
    """)


def test_local_dim_values_read_the_fact_column(con, defs):
    sql = compile_values(defs["waitlist"], "urgency")
    assert "clinic_dim" not in sql
    rows = [r[0] for r in con.execute(sql).fetchall()]
    assert rows == ["Cat 1", "Cat 2", "Cat 3"]


def test_values_behaviour_and_cascading(con, defs):
    from wh.metrics.result import BoundModel

    class FakeWs:
        def __init__(self, c):
            self.con = c
            self.config = type("Cfg", (), {"frames": None})()

        def _bind_warnings(self, model):
            return []

    m = BoundModel(defs["waitlist"], FakeWs(con))
    assert m.values("facility.region") == ["North", "South"]

    ctx = context(facility__region="North")
    assert m.values("facility.clinic", context=ctx) == [
        "Harbour Clinic", "Valley Clinic",
    ]
    # exclude-your-own-field: drop the entry and the full set returns
    assert m.values("facility.clinic", context=ctx.without("facility__region")) == [
        "Harbour Clinic", "Range Clinic", "Seaside Clinic", "Valley Clinic",
    ]


def test_widget_contexts_resolve_in_values(con, defs):
    from wh.metrics.result import BoundModel

    class FakeWs:
        def __init__(self, c):
            self.con = c
            self.config = type("Cfg", (), {"frames": None})()

        def _bind_warnings(self, model):
            return []

    class W:
        value = ["South"]

    m = BoundModel(defs["waitlist"], FakeWs(con))
    got = m.values("facility.clinic", context=context(facility__region=W()))
    assert got == ["Range Clinic", "Seaside Clinic"]


def test_unknown_attr_names_the_surface(defs):
    with pytest.raises(SemanticsError, match="facility"):
        compile_values(defs["waitlist"], "facility.galaxy")


def test_all_only_context_reads_the_dim_table_like_no_context(con, defs):
    from wh.metrics.compiler import compile_values
    from wh.metrics.context_ops import All, Context

    # a clinic no fact row references: only the dim-table lane can see it
    con.execute(
        "INSERT INTO main.clinic_dim VALUES "
        "('C9','Mountain Clinic','H9','Alpine','South')"
    )
    bare = [r[0] for r in con.execute(
        compile_values(defs["removals"], "facility.clinic")
    ).fetchall()]
    allctx = [r[0] for r in con.execute(
        compile_values(
            defs["removals"], "facility.clinic",
            Context({"facility__region": All()}),
        )
    ).fetchall()]
    assert allctx == bare
