"""The comparator must detect wrong answers, or every green is vacuous.
Plus pinned oracle behaviors on tiny hand-checked datasets."""

import pytest
from datetime import date

from oracle import assert_maps_equal, in_window, passes, period_of


def test_comparator_detects_wrong_value():
    with pytest.raises(AssertionError, match="compiled=2"):
        assert_maps_equal({(): {"n": 2}}, {(): {"n": 3}})


def test_comparator_detects_missing_and_extra_groups():
    with pytest.raises(AssertionError, match="group keys differ"):
        assert_maps_equal({("a",): {"n": 1}}, {("b",): {"n": 1}})


def test_comparator_detects_null_vs_zero():
    with pytest.raises(AssertionError):
        assert_maps_equal({(): {"amt": 0}}, {(): {"amt": None}})


def test_oracle_period_arithmetic_pinned():
    assert period_of(date(2026, 6, 30), "fy", 7) == date(2025, 7, 1)
    assert period_of(date(2026, 7, 1), "fy", 7) == date(2026, 7, 1)
    assert period_of(date(2026, 8, 15), "fy_quarter", 7) == date(2026, 7, 1)
    assert period_of(date(2026, 6, 15), "fy_quarter", 7) == date(2026, 4, 1)
    assert period_of(date(2026, 1, 1), "week", 1) == date(2025, 12, 29)
    assert period_of(date(2026, 1, 5), "week", 1) == date(2026, 1, 5)  # a Monday


def test_oracle_not_keeps_nulls():
    assert passes(None, ("not", "Cat 1"))
    assert not passes(None, ("eq", "Cat 1"))


def test_oracle_windows_are_day_inclusive():
    assert in_window(date(2026, 6, 30), (date(2026, 6, 1), date(2026, 6, 30)))
    assert not in_window(date(2026, 7, 1), (date(2026, 6, 1), date(2026, 6, 30)))
