from datetime import date

import pyarrow as pa
import pytest

from wh.errors import SemanticsError
from wh.metrics.context_ops import context, last
from wh.metrics.result import Slice


class FakeWidget:
    def __init__(self, v):
        self.value = v


def make(defs, con, model="removals", **kw):
    kw.setdefault("measures", ["removals"])
    return Slice(defs[model], con=lambda: con, **kw)


def test_sql_applied_ignored_exposed(defs, con):
    s = make(defs, con, ctx=context(facility__region="North", urgency="Cat 1"))
    assert "FILTER" in s.sql
    assert s.applied == ("facility__region",)
    assert s.ignored == ("urgency",)          # removals has no urgency dim


def test_strict_context_turns_ignored_into_errors(defs, con):
    with pytest.raises(SemanticsError, match="urgency"):
        make(defs, con, ctx=context(urgency="Cat 1"), strict_context=True)


def test_model_level_strict_context_and_per_slice_override(make_defs, design_yaml, con):
    strict_yaml = design_yaml["removals"].replace(
        "removals:\n  fact:", "removals:\n  strict_context: true\n  fact:"
    )
    defs = make_defs(design_yaml["dims"], strict_yaml)
    with pytest.raises(SemanticsError, match="urgency"):
        make(defs, con, ctx=context(urgency="Cat 1"))
    s = make(defs, con, ctx=context(urgency="Cat 1"), strict_context=False)
    assert s.ignored == ("urgency",)


def test_frame_executes_and_converts(defs, con):
    t = make(defs, con, by=["facility.region"]).frame(backend="pyarrow")
    assert isinstance(t, pa.Table)
    assert set(t.column_names) == {"facility.region", "removals"}


def test_view_registers_on_the_connection(defs, con):
    make(defs, con).view("junk_view")
    assert con.sql("SELECT removals FROM junk_view").fetchone() == (4,)


def test_widget_context_resolves_at_slice_time(defs, con):
    w = FakeWidget(["ENT"])
    s = make(defs, con, ctx=context(doctor__specialty=w))
    assert "'ENT'" in s.sql


def test_relative_time_anchors_to_fact_max_not_wall_clock(defs, con):
    # max removal_date is 2026-07-08; last 4 weeks = 06-11..07-08 -> 3 removals
    s = make(defs, con, ctx=context(time=last(4, "week")))
    t = s.frame(backend="pyarrow")
    assert t.column("removals").to_pylist() == [3]
    # hi compiles as the exclusive next-day bound of the 07-08 anchor
    assert "2026-06-11" in s.sql and "2026-07-09" in s.sql
