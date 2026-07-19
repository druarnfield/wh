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


def test_parquet_mode_creates_file_and_view(tmp_path):
    cfg = make_config(tmp_path, [make_spec("t2", mode="parquet")])
    build(cfg, fake_extract({"t2": {"b": ["x", "y"]}}), log=lambda s: None)

    pq = cfg.parquet_dir / "main" / "t2.parquet"
    assert pq.exists()
    assert q(cfg, 'SELECT b FROM "main"."t2" ORDER BY b') == [("x",), ("y",)]
    assert q(cfg, "SELECT mode, row_count FROM _mirror.meta") == [("parquet", 2)]


def test_failure_during_db_swap_restores_live_parquet(tmp_path, monkeypatch):
    # the realistic Windows case: live .duckdb locked by a notebook while
    # `wh mirror` runs, so the final os.replace fails AFTER the parquet dir
    # has been swapped — the old db must not end up serving new parquet data
    cfg = make_config(tmp_path, [make_spec("t1", mode="parquet")])
    build(cfg, fake_extract({"t1": {"a": [1]}}), log=lambda s: None)

    def boom(src, dst):
        raise OSError("file locked")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError):
        build(cfg, fake_extract({"t1": {"a": [2]}}), log=lambda s: None)
    monkeypatch.undo()

    # live db and its parquet files still serve the OLD data, no debris left
    assert q(cfg, 'SELECT a FROM "main"."t1"') == [(1,)]
    assert not cfg.parquet_dir.with_name("parquet.old").exists()
    assert not (tmp_path / ".mirror_staging").exists()


def test_failed_build_leaves_live_mirror_untouched(tmp_path):
    cfg = make_config(tmp_path, [make_spec("t1"), make_spec("t2")])
    build(cfg, fake_extract({"t1": {"a": [1]}, "t2": {"a": [2]}}), log=lambda s: None)

    def exploding(spec):
        if spec.name == "t2":
            raise RuntimeError("warehouse hiccup")
        import pyarrow as pa
        return pa.table({"a": [99]})

    with pytest.raises(RuntimeError):
        build(cfg, exploding, log=lambda s: None)

    # old data still intact, staging cleaned up
    assert q(cfg, 'SELECT a FROM "main"."t1"') == [(1,)]
    assert q(cfg, 'SELECT a FROM "main"."t2"') == [(2,)]
    assert not (tmp_path / ".mirror_staging").exists()
