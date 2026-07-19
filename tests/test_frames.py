import duckdb
import pandas as pd
import polars as pl
import pyarrow as pa
import pytest

from wh.errors import WhError
from wh.frames import default_backend, from_arrow, to_arrow

TABLE = pa.table({"a": [1, 2]})


def test_default_backend_prefers_polars():
    # polars is installed in the dev environment
    assert default_backend() == "polars"


@pytest.mark.parametrize("obj", [
    TABLE,
    pl.DataFrame({"a": [1, 2]}),
    pd.DataFrame({"a": [1, 2]}),
])
def test_to_arrow_roundtrips(obj):
    out = to_arrow(obj)
    assert isinstance(out, pa.Table)
    assert out.column("a").to_pylist() == [1, 2]


def test_to_arrow_duckdb_relation():
    con = duckdb.connect()
    rel = con.sql("SELECT 1 AS a UNION ALL SELECT 2 ORDER BY a")
    assert to_arrow(rel).column("a").to_pylist() == [1, 2]


def test_to_arrow_rejects_junk():
    with pytest.raises(WhError, match="not a supported"):
        to_arrow({"a": [1, 2]})


def test_to_arrow_record_batch_reader():
    batch = pa.RecordBatch.from_pydict({"a": [1, 2]})
    reader = pa.RecordBatchReader.from_batches(batch.schema, [batch])
    assert to_arrow(reader).column("a").to_pylist() == [1, 2]


def test_to_arrow_lazyframe_hint():
    lf = pl.LazyFrame({"a": [1]})
    with pytest.raises(WhError, match="collect"):
        to_arrow(lf)


def test_from_arrow_backends():
    assert isinstance(from_arrow(TABLE, "polars"), pl.DataFrame)
    assert isinstance(from_arrow(TABLE, "pandas"), pd.DataFrame)
    assert from_arrow(TABLE, "pyarrow") is TABLE


def test_from_arrow_unknown_backend():
    with pytest.raises(WhError, match="backend"):
        from_arrow(TABLE, "spark")
