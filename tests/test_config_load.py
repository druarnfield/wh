import pytest
import yaml

from wh.config import load_config
from wh.errors import ConfigError

VALID = {
    "sources": {
        "warehouse": {
            "driver": "mssql",
            "server": "localhost,1433",
            "database": "ExecReporting",
            "auth": {"user": "sa", "password_env": "WH_PWD"},
        },
        "other": {"driver": "mssql", "dsn_env": "OTHER_DSN"},
    },
    "destination": {"duckdb_path": "./metrics.duckdb"},
    "defaults": {"schema_in_duckdb": "core", "mode": "native"},
    "tables": [
        {
            "name": "waitlist",
            "description": "Current waitlist",
            "source": {"database": "ExecReporting", "schema": "dbo", "table": "Waits"},
        },
        {
            "name": "snapshot",
            "schema_in_duckdb": "main",
            "mode": "parquet",
            "source": {"query": "SELECT 1 AS x"},
        },
    ],
}


def write(tmp_path, cfg_dict):
    p = tmp_path / "wh.yaml"
    p.write_text(yaml.safe_dump(cfg_dict, sort_keys=False))
    return p


def test_valid_config(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert set(cfg.sources) == {"warehouse", "other"}
    assert cfg.default_source == "warehouse"          # first listed
    assert cfg.duckdb_path == (tmp_path / "metrics.duckdb").resolve()
    assert cfg.parquet_dir == (tmp_path / "parquet").resolve()  # default: sibling
    t0, t1 = cfg.tables
    assert (t0.name, t0.schema_in_duckdb, t0.mode) == ("waitlist", "core", "native")
    assert t0.source.sql() == "SELECT * FROM [ExecReporting].[dbo].[Waits]"
    assert (t1.name, t1.schema_in_duckdb, t1.mode) == ("snapshot", "main", "parquet")
    assert t1.source.sql() == "SELECT 1 AS x"


def test_relative_paths_resolve_against_config_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path.parent)  # cwd != config dir
    cfg = load_config(write(tmp_path, VALID))
    assert cfg.duckdb_path.parent == tmp_path


def test_no_sources_raises(tmp_path):
    bad = {**VALID, "sources": {}}
    with pytest.raises(ConfigError, match="source"):
        load_config(write(tmp_path, bad))


def test_unknown_driver_raises(tmp_path):
    bad = {**VALID, "sources": {"w": {"driver": "postgres", "dsn_env": "X"}}}
    with pytest.raises(ConfigError, match="driver"):
        load_config(write(tmp_path, bad))


def test_table_query_and_ref_mutually_exclusive(tmp_path):
    bad = dict(VALID)
    bad["tables"] = [{
        "name": "x",
        "source": {"query": "SELECT 1", "database": "a", "schema": "b", "table": "c"},
    }]
    with pytest.raises(ConfigError, match="either"):
        load_config(write(tmp_path, bad))


def test_duplicate_table_raises(tmp_path):
    bad = dict(VALID)
    bad["tables"] = [VALID["tables"][0], VALID["tables"][0]]
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(write(tmp_path, bad))


def test_no_tables_raises(tmp_path):
    bad = {**VALID, "tables": []}
    with pytest.raises(ConfigError, match="no tables"):
        load_config(write(tmp_path, bad))


def test_frames_default_is_none(tmp_path):
    assert load_config(write(tmp_path, VALID)).frames is None


def test_frames_parsed(tmp_path):
    cfg_dict = {**VALID, "defaults": {**VALID["defaults"], "frames": "pandas"}}
    assert load_config(write(tmp_path, cfg_dict)).frames == "pandas"


def test_frames_invalid(tmp_path):
    cfg_dict = {**VALID, "defaults": {**VALID["defaults"], "frames": "spark"}}
    with pytest.raises(ConfigError, match="frames"):
        load_config(write(tmp_path, cfg_dict))


def test_semantics_dir_default(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert cfg.semantics_dir == tmp_path / "semantics"


def test_semantics_dir_custom(tmp_path):
    d = {**VALID, "semantics": {"dir": "./defs"}}
    assert load_config(write(tmp_path, d)).semantics_dir == tmp_path / "defs"


def test_push_allow_default_empty(tmp_path):
    assert load_config(write(tmp_path, VALID)).push_allow == []


def test_push_allow_parsed(tmp_path):
    cfg_dict = {**VALID, "push": {"allow": ["Sandbox.dbo", "Sandbox.analysis"]}}
    assert load_config(write(tmp_path, cfg_dict)).push_allow == [
        "Sandbox.dbo", "Sandbox.analysis"
    ]


def test_push_allow_entry_must_be_two_part(tmp_path):
    for bad in ("Sandbox", "Sandbox.", ".dbo", 1.5):
        cfg_dict = {**VALID, "push": {"allow": [bad]}}
        with pytest.raises(ConfigError, match="Database.schema"):
            load_config(write(tmp_path, cfg_dict))


def test_missing_file_raises_configerror(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "nope" / "wh.yaml")


def test_malformed_yaml_raises_configerror(tmp_path):
    p = tmp_path / "wh.yaml"
    p.write_text("sources: [unclosed")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(p)


def test_bad_compression_raises(tmp_path):
    bad = dict(VALID)
    bad["tables"] = [{
        "name": "x",
        "compression": "zstandard",  # typo for zstd
        "source": {"query": "SELECT 1"},
    }]
    with pytest.raises(ConfigError, match="compression"):
        load_config(write(tmp_path, bad))


def test_fiscal_year_start_default_is_calendar(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert cfg.fiscal_year_start == 1


def test_fiscal_year_start_parsed(tmp_path):
    d = {**VALID, "semantics": {"fiscal_year_start": 7}}
    assert load_config(write(tmp_path, d)).fiscal_year_start == 7


@pytest.mark.parametrize("bad", [0, 13, "july", True])
def test_fiscal_year_start_invalid(tmp_path, bad):
    d = {**VALID, "semantics": {"fiscal_year_start": bad}}
    with pytest.raises(ConfigError, match="fiscal_year_start"):
        load_config(write(tmp_path, d))
