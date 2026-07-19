import duckdb
import pytest

from wh.mirror import build
from tests.conftest import make_config, make_spec, fake_extract


def q(cfg, sql):
    con = duckdb.connect(str(cfg.duckdb_path), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_native_table_lands_with_meta(tmp_path):
    cfg = make_config(tmp_path, [make_spec("t1")])
    build(cfg, fake_extract({"t1": {"a": [1, 2, 3]}}), log=lambda s: None)

    assert q(cfg, 'SELECT a FROM "main"."t1" ORDER BY a') == [(1,), (2,), (3,)]
    meta = q(cfg, "SELECT schema_name, table_name, mode, row_count FROM _mirror.meta")
    assert meta == [("main", "t1", "native", 3)]
    ts = q(cfg, "SELECT extracted_at FROM _mirror.meta")[0][0]
    assert ts is not None


def test_description_becomes_comment(tmp_path):
    cfg = make_config(tmp_path, [make_spec("t1", description="my table")])
    build(cfg, fake_extract({"t1": {"a": [1]}}), log=lambda s: None)
    rows = q(cfg, "SELECT comment FROM duckdb_tables() WHERE table_name = 't1'")
    assert rows == [("my table",)]


def test_staging_cleaned_up(tmp_path):
    cfg = make_config(tmp_path, [make_spec("t1")])
    build(cfg, fake_extract({"t1": {"a": [1]}}), log=lambda s: None)
    assert not (tmp_path / ".mirror_staging").exists()
