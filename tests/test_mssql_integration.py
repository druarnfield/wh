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


def test_push_roundtrip(tmp_path):
    import polars as pl
    import yaml
    import wh

    cfg = {
        "sources": {"warehouse": {"driver": "mssql", "dsn_env": "WH_TEST_DSN"}},
        "destination": {"duckdb_path": "./t.duckdb"},
        "push": {"allow": ["ExecReporting.dbo"]},
        "tables": [{"name": "x", "source": {"query": "SELECT 1 AS a"}}],
    }
    (tmp_path / "wh.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    ws = wh.workspace(tmp_path / "wh.yaml")

    from datetime import datetime, timedelta, timezone

    perth = timezone(timedelta(hours=8))
    ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=perth)
    df = pl.DataFrame(
        {
            "n": [1, 2, 3],
            "s": ["x", "y", None],
            "ts": pl.Series([ts] * 3, dtype=pl.Datetime("us", "+08:00")),
            "cat": pl.Series(["a", "b", "a"], dtype=pl.Categorical),
        }
    )
    target = "ExecReporting.dbo.wh_push_test"
    try:
        assert ws.push(df, target) == 3
        assert ws.push(df, target, if_exists="replace") == 3
        with pytest.raises(wh.WhError, match="replace"):
            ws.push(df, target)
        back = ws.pull(
            "SELECT n, s, CAST(cat AS NVARCHAR(10)) AS cat, "
            "DATEPART(TZOFFSET, ts) AS tz_minutes "
            "FROM [ExecReporting].[dbo].[wh_push_test] ORDER BY n"
        )
        assert back["n"].to_list() == [1, 2, 3]
        assert back["s"].to_list() == ["x", "y", None]
        assert back["cat"].to_list() == ["a", "b", "a"]
        assert back["tz_minutes"].to_list() == [480, 480, 480]   # offset kept
        with pytest.raises(wh.PushRefused):
            ws.push(df, "ExecReporting.other_schema.t")
    finally:
        conn = None
        try:
            from wh.sources.mssql import open_connection

            conn = open_connection(ws.config.sources["warehouse"])
            cur = conn.cursor()
            cur.execute("DROP TABLE IF EXISTS [ExecReporting].[dbo].[wh_push_test]")
            conn.commit()
        finally:
            if conn is not None:
                conn.close()
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
