"""The frame boundary: narwhals in, Arrow through the core, your backend out.

The library never depends on pandas or polars directly — narwhals accepts
whichever the host project uses, and `from_arrow` hands results back in the
preferred backend (config `defaults.frames`, else best available).
"""

from __future__ import annotations

import importlib.util

import duckdb
import narwhals as nw
import pyarrow as pa

from .errors import WhError

VALID_BACKENDS = ("polars", "pandas", "pyarrow")


def default_backend() -> str:
    for name in ("polars", "pandas"):
        if importlib.util.find_spec(name) is not None:
            return name
    return "pyarrow"


def to_arrow(obj) -> pa.Table:
    """Any supported frame-like object -> pyarrow Table."""
    if isinstance(obj, pa.Table):
        return obj
    if isinstance(obj, pa.RecordBatchReader):
        return obj.read_all()
    if isinstance(obj, pa.RecordBatch):
        return pa.Table.from_batches([obj])
    if isinstance(obj, duckdb.DuckDBPyRelation):
        return obj.to_arrow_table()
    if hasattr(obj, "__arrow_c_stream__"):   # PyCapsule stream interface
        return pa.RecordBatchReader.from_stream(obj).read_all()
    if hasattr(obj, "to_pyarrow"):          # ibis expressions, BSL queries
        return obj.to_pyarrow()
    try:
        return nw.from_native(obj, eager_only=True).to_arrow()
    except TypeError as e:
        if obj.__class__.__name__ == "LazyFrame":
            raise WhError(
                "that's a polars LazyFrame — call .collect() first"
            ) from e
        raise WhError(
            f"{type(obj).__name__} is not a supported frame type "
            f"(pandas/polars/pyarrow/duckdb relation)"
        ) from e


def from_arrow(table: pa.Table, backend: str):
    if backend == "pyarrow":
        return table
    if backend == "polars":
        import polars as pl

        return pl.from_arrow(table)
    if backend == "pandas":
        return table.to_pandas()
    raise WhError(f"unknown frames backend '{backend}' (use one of {VALID_BACKENDS})")
