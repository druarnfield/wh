import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("WH_TEST_DSN"),
    reason="set WH_TEST_DSN to run SQL Server integration tests",
)


def test_pull_and_land_roundtrip(tmp_path):
    import yaml
    import wh

    cfg = {
        "sources": {"warehouse": {"driver": "mssql", "dsn_env": "WH_TEST_DSN"}},
        "destination": {"duckdb_path": "./t.duckdb"},
        "tables": [{"name": "x", "source": {"query": "SELECT 1 AS a"}}],
    }
    (tmp_path / "wh.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    ws = wh.workspace(tmp_path / "wh.yaml")

    df = ws.pull("SELECT 2 + 2 AS four")
    assert df["four"].to_list() == [4]

    assert ws.land("SELECT 1 AS n UNION ALL SELECT 2", table="scratch.nums") == 2
    assert ws.con.execute('SELECT sum(n) FROM "scratch"."nums"').fetchone() == (3,)
    ws.close()


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
