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


class _Clean:
    """Callable pipeline runner that also namespaces the step functions."""

    snake_names = staticmethod(snake_names)
    drop_empty = staticmethod(drop_empty)
    strip_strings = staticmethod(strip_strings)

    def __call__(self, df, *steps):
        ndf = nw.from_native(df, eager_only=True)
        for step in steps:
            ndf = step(ndf)
        return ndf.to_native()


clean = _Clean()
