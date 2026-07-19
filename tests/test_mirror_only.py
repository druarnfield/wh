import duckdb
import pytest

from wh.errors import ConfigError
from wh.mirror import build
from tests.conftest import make_config, make_spec, fake_extract
from tests.test_mirror import q


def test_only_refreshes_selected_and_carries_rest(tmp_path):
    cfg = make_config(tmp_path, [make_spec("t1"), make_spec("t2")])
    build(cfg, fake_extract({"t1": {"a": [1]}, "t2": {"a": [10]}}), log=lambda s: None)
    first_meta = dict(q(cfg, "SELECT table_name, extracted_at FROM _mirror.meta"))

    build(cfg, fake_extract({"t1": {"a": [2]}}), only=["t1"], log=lambda s: None)

    assert q(cfg, 'SELECT a FROM "main"."t1"') == [(2,)]      # refreshed
    assert q(cfg, 'SELECT a FROM "main"."t2"') == [(10,)]     # carried over
    second_meta = dict(q(cfg, "SELECT table_name, extracted_at FROM _mirror.meta"))
    assert second_meta["t2"] == first_meta["t2"]              # meta preserved
    assert second_meta["t1"] > first_meta["t1"]               # meta refreshed


def test_only_carries_parquet_files(tmp_path):
    cfg = make_config(
        tmp_path, [make_spec("t1"), make_spec("t2", mode="parquet")]
    )
    build(cfg, fake_extract({"t1": {"a": [1]}, "t2": {"b": ["x"]}}), log=lambda s: None)

    build(cfg, fake_extract({"t1": {"a": [2]}}), only=["t1"], log=lambda s: None)

    assert (cfg.parquet_dir / "main" / "t2.parquet").exists()
    assert q(cfg, 'SELECT b FROM "main"."t2"') == [("x",)]


def test_only_extracts_table_missing_from_previous(tmp_path):
    cfg1 = make_config(tmp_path, [make_spec("t1")])
    build(cfg1, fake_extract({"t1": {"a": [1]}}), log=lambda s: None)

    # config grows a new table; --only t1 must still produce a complete mirror
    cfg2 = make_config(tmp_path, [make_spec("t1"), make_spec("t_new")])
    build(
        cfg2,
        fake_extract({"t1": {"a": [2]}, "t_new": {"a": [7]}}),
        only=["t1"],
        log=lambda s: None,
    )

    assert q(cfg2, 'SELECT a FROM "main"."t_new"') == [(7,)]


def test_only_without_existing_mirror_is_full_build(tmp_path):
    cfg = make_config(tmp_path, [make_spec("t1"), make_spec("t2")])
    build(
        cfg,
        fake_extract({"t1": {"a": [1]}, "t2": {"a": [2]}}),
        only=["t1"],
        log=lambda s: None,
    )
    assert q(cfg, 'SELECT a FROM "main"."t2"') == [(2,)]


def test_only_unknown_name_raises(tmp_path):
    cfg = make_config(tmp_path, [make_spec("t1")])
    with pytest.raises(ConfigError, match="nope"):
        build(cfg, fake_extract({"t1": {"a": [1]}}), only=["nope"], log=lambda s: None)
