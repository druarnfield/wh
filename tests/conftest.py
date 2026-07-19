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


@pytest.fixture
def messy_xlsx(tmp_path):
    """A realistically messy workbook: title rows, gap column, mixed
    number-as-text column, second sheet."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["Acme Health — Waitlist Extract"])
    ws.append([])
    ws.append(["UR", "Referral Date", None, "Days Waiting"])
    ws.append(["A1", "2026-01-01", None, "1,234"])
    ws.append(["A2", "2026-01-05", None, 8])
    wb.create_sheet("Notes").append(["ignore me"])
    p = tmp_path / "messy.xlsx"
    wb.save(p)
    return p


@pytest.fixture
def semantic_project(project):
    """project + a built mini-mirror (waitlist, files.clinics) + model YAML."""
    import duckdb

    con = duckdb.connect(str(project / "metrics.duckdb"))
    con.execute(
        "CREATE TABLE waitlist AS SELECT * FROM (VALUES "
        "('U1','Cardio','C1',40),('U2','Cardio','C2',10),('U3','Ortho','C1',60)"
        ") t(patient_ur, specialty, clinic_code, wait_days)"
    )
    con.execute("CREATE SCHEMA files")
    con.execute(
        "CREATE TABLE files.clinics AS SELECT * FROM (VALUES "
        "('C1','North'),('C2','South')) t(code, region)"
    )
    con.close()
    sdir = project / "semantics"
    sdir.mkdir()
    (sdir / "waitlist.yml").write_text(
        "waitlist:\n"
        "  table: waitlist\n"
        "  dimensions:\n"
        "    specialty: _.specialty\n"
        "    clinic:\n"
        "      expr: _.clinic_code\n"
        "      is_entity: true\n"
        "  measures:\n"
        "    patients_waiting: _.patient_ur.nunique()\n"
        "    median_wait_days: _.wait_days.median()\n"
    )
    (sdir / "clinics.yml").write_text(
        "clinics:\n"
        "  table: files.clinics\n"
        "  dimensions:\n"
        "    code:\n"
        "      expr: _.code\n"
        "      is_entity: true\n"
        "    region: _.region\n"
        "  measures:\n"
        "    n_clinics: _.count()\n"
    )
    return project


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
