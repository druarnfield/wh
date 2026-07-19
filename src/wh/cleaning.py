"""Composable cleaners for messy business data.

    df = wh.clean(raw, wh.clean.snake_names, wh.clean.drop_empty,
                  wh.clean.strip_strings, wh.clean.parse_dates("referral_date"),
                  wh.clean.numeric("wait_days"))

Steps are narwhals DataFrame -> DataFrame functions, so they work on polars
and pandas alike; `clean()` returns the same frame type it was given.
(Module named `cleaning`, not `clean`, to avoid the package-attribute
shadowing gotcha — see CLAUDE.md.)
"""

from __future__ import annotations

import re
from datetime import datetime

import narwhals as nw


def _snake(name: str) -> str:
    return re.sub(r"[^0-9a-zA-Z]+", "_", str(name).strip()).strip("_").lower() or "col"


def snake_names(df: nw.DataFrame) -> nw.DataFrame:
    mapping, used = {}, set()
    for c in df.columns:
        base = _snake(c)
        n, i = base, 1
        while n in used:
            i += 1
            n = f"{base}_{i}"
        used.add(n)
        mapping[c] = n
    return df.rename(mapping)


def drop_empty(df: nw.DataFrame) -> nw.DataFrame:
    if len(df) == 0:
        return df                       # nothing to judge; keep the schema
    keep = [c for c in df.columns if df.get_column(c).null_count() < len(df)]
    df = df.select(keep)
    if df.columns:
        df = df.filter(
            ~nw.all_horizontal(
                *[nw.col(c).is_null() for c in df.columns], ignore_nulls=False
            )
        )
    return df


def strip_strings(df: nw.DataFrame) -> nw.DataFrame:
    cols = [c for c in df.columns if df.schema[c] == nw.String]
    if not cols:
        return df
    return df.with_columns(
        *[
            nw.when(nw.col(c).str.strip_chars() == "")
            .then(None)
            .otherwise(nw.col(c).str.strip_chars())
            .alias(c)
            for c in cols
        ]
    )


_EXCEL_EPOCH = datetime(1899, 12, 30)
_US_PER_DAY = 86_400_000_000


def parse_dates(*cols: str, format: str | None = None):
    """Parse string dates (with `format`, e.g. '%d/%m/%Y') and numeric Excel
    serials into datetimes. Datetime columns pass through untouched.
    (Serial conversion is polars-tested; see tests.)"""

    def step(df: nw.DataFrame) -> nw.DataFrame:
        exprs = []
        for c in cols:
            dtype = df.schema[c]
            if dtype == nw.String:
                exprs.append(nw.col(c).str.to_datetime(format=format).alias(c))
            elif dtype.is_numeric():
                exprs.append(
                    (
                        (nw.col(c) * _US_PER_DAY)
                        .cast(nw.Int64)
                        .cast(nw.Duration(time_unit="us"))
                        + nw.lit(_EXCEL_EPOCH)
                    ).alias(c)
                )
            # datetime/date already: leave alone
        return df.with_columns(*exprs) if exprs else df

    return step


_NUMERIC_RE = r"^-?(?:\d+\.?\d*|\.\d+)$"


def numeric(*cols: str):
    """Coerce messy string numbers ('1,234', '$5.50', '-', '') to Float64.
    Anything that isn't a number after stripping currency symbols/commas
    ('N/A', 'TBC', ...) becomes null rather than raising."""

    def step(df: nw.DataFrame) -> nw.DataFrame:
        exprs = []
        for c in cols:
            if df.schema[c] != nw.String:
                continue
            stripped = nw.col(c).str.strip_chars().str.replace_all(r"[$€£,\s]", "")
            exprs.append(
                nw.when(stripped.str.contains(_NUMERIC_RE))
                .then(stripped)
                .otherwise(None)
                .cast(nw.Float64)
                .alias(c)
            )
        return df.with_columns(*exprs) if exprs else df

    return step


class _Clean:
    """Callable pipeline runner that also namespaces the step functions."""

    snake_names = staticmethod(snake_names)
    drop_empty = staticmethod(drop_empty)
    strip_strings = staticmethod(strip_strings)
    parse_dates = staticmethod(parse_dates)
    numeric = staticmethod(numeric)

    def __call__(self, df, *steps):
        ndf = nw.from_native(df, eager_only=True)
        for step in steps:
            ndf = step(ndf)
        return ndf.to_native()


clean = _Clean()
