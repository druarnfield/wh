"""marimo widget helpers: thin wrappers over values() and the fact bounds."""

from datetime import date

import pytest

from wh.errors import SemanticsError
from wh.metrics.compiler import compile_slice
from wh.metrics.context_ops import context
from wh.metrics.result import BoundModel

mo = pytest.importorskip("marimo")


class FakeWs:
    def __init__(self, c):
        self.con = c
        self.config = type("Cfg", (), {"frames": None})()

    def _bind_warnings(self, model):
        return []


@pytest.fixture
def bound(con, defs):
    return BoundModel(defs["waitlist"], FakeWs(con))


def test_filter_dim_is_a_populated_multiselect(bound):
    w = bound.filter_dim("facility.region")
    assert isinstance(w, mo.ui.multiselect)
    assert sorted(w.options) == ["North", "South"]
    assert w.value == []                     # empty selection -> unfiltered


def test_filter_dim_cascades_through_a_context(bound):
    w = bound.filter_dim("facility.clinic", context=context(facility__region="North"))
    assert sorted(w.options) == ["Harbour Clinic", "Valley Clinic"]


def test_empty_filter_dim_widget_slices_unfiltered(bound, con, defs):
    w = bound.filter_dim("facility.region")
    c = compile_slice(
        defs["waitlist"], ["patients_waiting"],
        ctx=context(facility__region=w).resolve(anchor=date(2026, 7, 10)),
    )
    assert "WHERE" not in c.sql.split("__asat")[-1]   # no attribute predicate emitted
    assert c.applied == ("facility__region",)


def test_filter_date_carries_real_bounds(bound):
    w = bound.filter_date()
    assert isinstance(w, mo.ui.date_range)
    assert w.value == (date(2026, 6, 5), date(2026, 7, 10))


def test_filter_date_value_is_a_valid_time_context(bound, con, defs):
    w = bound.filter_date()
    ctx = context(time=w).resolve(anchor=date(2026, 7, 10))
    rows = con.execute(
        compile_slice(defs["waitlist"], ["patients_waiting"], ctx=ctx).sql
    ).fetchall()
    assert rows == [(4,)]                    # full range: as-at 07-10


def test_missing_marimo_error_names_the_fix(bound, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_marimo(name, *a, **kw):
        if name == "marimo":
            raise ImportError("No module named 'marimo'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_marimo)
    with pytest.raises(SemanticsError, match="marimo"):
        bound.filter_dim("facility.region")
