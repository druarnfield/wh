from datetime import date, datetime

import pandas as pd
import polars as pl
import pytest

from wh.cleaning import clean


@pytest.fixture(params=["polars", "pandas"])
def make_frame(request):
    def _make(data: dict):
        return pl.DataFrame(data) if request.param == "polars" else pd.DataFrame(data)
    return _make


def as_dict(df) -> dict:
    if isinstance(df, pl.DataFrame):
        return {k: list(v) for k, v in df.to_dict(as_series=False).items()}
    return {k: [None if pd.isna(x) else x for x in v] for k, v in df.to_dict("list").items()}


def test_clean_returns_same_backend(make_frame):
    df = make_frame({"A": [1]})
    out = clean(df)
    assert type(out) is type(df)


def test_snake_names(make_frame):
    df = make_frame({" Referral Date ": [1], "Seen (Days)": [2], "x": [3], "X ": [4]})
    out = clean(df, clean.snake_names)
    assert list(as_dict(out)) == ["referral_date", "seen_days", "x", "x_2"]


def test_drop_empty(make_frame):
    df = make_frame({"a": [1, None, None], "b": ["x", None, "y"], "junk": [None, None, None]})
    out = clean(df, clean.drop_empty)
    d = as_dict(out)
    assert "junk" not in d
    assert d["a"] == [1, None]           # middle all-null row dropped
    assert d["b"] == ["x", "y"]


def test_strip_strings(make_frame):
    df = make_frame({"s": ["  x ", "", "   ", "y"], "n": [1, 2, 3, 4]})
    out = clean(df, clean.strip_strings)
    assert as_dict(out)["s"] == ["x", None, None, "y"]
    assert as_dict(out)["n"] == [1, 2, 3, 4]


def test_steps_compose_in_order(make_frame):
    df = make_frame({" A ": ["  v  ", None], "junk": [None, None]})
    out = clean(df, clean.snake_names, clean.drop_empty, clean.strip_strings)
    assert as_dict(out) == {"a": ["v"]}


def test_parse_dates_strings(make_frame):
    df = make_frame({"d": ["2026-01-02", None]})
    out = clean(df, clean.parse_dates("d", format="%Y-%m-%d"))
    assert as_dict(out)["d"][0] == datetime(2026, 1, 2)


def test_parse_dates_excel_serials_polars():
    # serial 45658 = 2025-01-01 (origin 1899-12-30)
    df = pl.DataFrame({"d": [45658.0, None]})
    out = clean(df, clean.parse_dates("d"))
    assert out["d"][0] == datetime(2025, 1, 1)


def test_parse_dates_passthrough_datetime(make_frame):
    df = make_frame({"d": [datetime(2026, 1, 1)]})
    out = clean(df, clean.parse_dates("d"))
    assert as_dict(out)["d"] == [datetime(2026, 1, 1)]


def test_numeric(make_frame):
    df = make_frame({"v": ["1,234", "$5.50", " 7 ", "-", "", None]})
    out = clean(df, clean.numeric("v"))
    assert as_dict(out)["v"] == [1234.0, 5.5, 7.0, None, None, None]


def test_numeric_leaves_numbers(make_frame):
    df = make_frame({"v": [1.5, 2.0]})
    out = clean(df, clean.numeric("v"))
    assert as_dict(out)["v"] == [1.5, 2.0]
