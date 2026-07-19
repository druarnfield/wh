from pathlib import Path

import pyarrow as pa
import pytest
import yaml

from wh.config import Auth, Config, Source, SourceRef, TableSpec


@pytest.fixture
def project(tmp_path):
    """A minimal project dir with a valid wh.yaml. Returns its path."""
    cfg = {
        "sources": {
            "warehouse": {
                "driver": "mssql",
                "server": "localhost,1433",
                "database": "Db",
                "auth": {"user": "sa", "password_env": "WH_PWD"},
            }
        },
        "destination": {"duckdb_path": "./metrics.duckdb"},
        "tables": [
            {"name": "t1", "source": {"query": "SELECT 1 AS a"}},
        ],
    }
    (tmp_path / "wh.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    return tmp_path


def make_spec(name, schema="main", mode="native", **kw):
    return TableSpec(
        name=name, source=SourceRef(query=f"SELECT * FROM {name}"),
        schema_in_duckdb=schema, mode=mode, **kw,
    )


def make_config(tmp_path, specs):
    return Config(
        path=tmp_path / "wh.yaml",
        sources={"warehouse": Source(name="warehouse", driver="mssql", dsn_env="X")},
        default_source="warehouse",
        duckdb_path=tmp_path / "metrics.duckdb",
        parquet_dir=tmp_path / "parquet",
        tables=specs,
    )


def fake_extract(data: dict):
    """extract seam returning canned pyarrow tables by spec name."""
    def _extract(spec):
        return pa.table(data[spec.name])
    return _extract
