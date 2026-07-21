from datetime import date

import pytest

from wh.errors import SemanticsError
from wh.metrics.context_ops import (
    All, Between, Eq, In, Not, all_, context, last, not_,
)


class FakeWidget:  # anything with .value — the marimo duck-type
    def __init__(self, v):
        self.value = v


def test_context_basics_and_immutability():
    ctx = context(facility__region="North", doctor__specialty=["ENT", "Ophthal"])
    assert ctx.entries["facility__region"] == Eq("North")
    assert ctx.entries["doctor__specialty"] == In(("ENT", "Ophthal"))
    ctx2 = ctx.with_(facility__district="Coastal")
    assert "facility__district" not in ctx.entries
    assert ctx2.entries["facility__district"] == Eq("Coastal")
    assert ctx.without("doctor__specialty").entries.keys() == {"facility__region"}
    with pytest.raises(AttributeError):
        ctx.entries = {}


def test_time_range_and_ops():
    ctx = context(time=("2025-07-01", "2026-06-30"), urgency=not_("Cat 3"))
    assert ctx.entries["time"] == Between(date(2025, 7, 1), date(2026, 6, 30))
    assert ctx.entries["urgency"] == Not("Cat 3")


def test_merge_right_wins_per_attribute():
    merged = context(facility__region="North", urgency="Cat 1") | context(
        facility__region="South"
    )
    assert merged.entries["facility__region"] == Eq("South")
    assert merged.entries["urgency"] == Eq("Cat 1")


def test_literal_empty_list_is_an_error():
    with pytest.raises(SemanticsError, match="wh.all"):
        context(facility__region=[])


def test_widget_resolution_at_resolve_time():
    w = FakeWidget(["ENT"])
    ctx = context(doctor__specialty=w)
    w.value = ["ENT", "Ophthal"]  # changed after construction
    resolved = ctx.resolve(anchor=date(2026, 7, 19))
    assert resolved.entries["doctor__specialty"] == In(("ENT", "Ophthal"))


def test_empty_widget_means_unfiltered():
    r = context(doctor__specialty=FakeWidget([])).resolve(anchor=date(2026, 7, 19))
    assert r.entries["doctor__specialty"] == All()
    assert context(facility__region=all_()).entries["facility__region"] == All()


def test_relative_time_resolves_against_anchor_not_wall_clock():
    r = context(time=last(12, "month")).resolve(anchor=date(2026, 7, 14))
    assert r.entries["time"] == Between(date(2025, 7, 15), date(2026, 7, 14))
    r2 = context(time=last(4, "week")).resolve(anchor=date(2026, 7, 14))
    assert r2.entries["time"] == Between(date(2026, 6, 17), date(2026, 7, 14))


def test_resolved_context_is_plain_data():
    r = context(
        facility__region="North", time=("2025-07-01", "2026-06-30")
    ).resolve(anchor=date(2026, 7, 19))
    assert r.is_resolved
    assert isinstance(hash(r), int)
    assert r.to_dict() == {
        "facility__region": {"eq": "North"},
        "time": {"between": ["2025-07-01", "2026-06-30"]},
    }


def test_unresolved_context_refuses_to_be_data():
    ctx = context(doctor__specialty=FakeWidget(["ENT"]))
    assert not ctx.is_resolved
    with pytest.raises(SemanticsError, match="resolve"):
        ctx.to_dict()


def test_context_values_must_be_scalars():
    with pytest.raises(SemanticsError, match="NULL"):
        context(facility__region=None)
    with pytest.raises(SemanticsError, match="scalars"):
        context(facility__region=[["North"], "South"])   # nested list slip
    with pytest.raises(SemanticsError, match="non-finite"):
        context(urgency=float("nan"))
    with pytest.raises(SemanticsError, match="wh.all"):
        context(facility__region=In(()))                 # empty op, direct


def test_resolve_is_idempotent_and_plain_contexts_are_born_resolved():
    ctx = context(facility__region="North")
    assert ctx.is_resolved
    assert ctx.resolve(anchor=date(2026, 1, 1)).entries == ctx.entries


def test_last_rejects_zero_and_negative_n():
    with pytest.raises(SemanticsError, match="n >= 1"):
        last(0, "day")
    with pytest.raises(SemanticsError, match="n >= 1"):
        last(-3, "month")
