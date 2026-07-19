import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("WH_TEST_DSN"),
    reason="set WH_TEST_DSN to run SQL Server integration tests",
)


def test_extract_roundtrip(tmp_path):
    from wh.config import Source, SourceRef, TableSpec
    from wh.sources.mssql import MssqlExtractor
    import pyarrow as pa

    src = Source(name="test", driver="mssql", dsn_env="WH_TEST_DSN")
    ex = MssqlExtractor(src)
    try:
        spec = TableSpec(name="probe", source=SourceRef(query="SELECT 1 AS n"))
        result = ex(spec)
        table = result if isinstance(result, pa.Table) else pa.table(result)
        assert table.column("n").to_pylist() == [1]
    finally:
        ex.close()
