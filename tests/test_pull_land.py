import pandas as pd
import polars as pl
import pyarrow as pa
import pytest

import wh
from wh.errors import ConfigError, WhError
from wh.workspace import Workspace


@pytest.fixture
def fake_mssql(monkeypatch):
    """Patch MssqlExtractor with a fake serving canned queries."""
    import wh.sources.mssql as mssql_mod

    class FakeExtractor:
        def __init__(self, source):
            self.source = source

        def query(self, sql, batch_size=100_000):
            return pa.table({"n": [1, 2, 3], "q": [sql] * 3})

        def close(self):
            pass

    monkeypatch.setattr(mssql_mod, "MssqlExtractor", FakeExtractor)
    return FakeExtractor


def test_pull_returns_default_backend(project, fake_mssql):
    ws = Workspace.load(project / "wh.yaml")
    df = ws.pull("SELECT 1")
    assert isinstance(df, pl.DataFrame)      # polars installed -> default
    assert df["n"].to_list() == [1, 2, 3]


def test_pull_backend_override(project, fake_mssql):
    ws = Workspace.load(project / "wh.yaml")
    assert isinstance(ws.pull("SELECT 1", backend="pandas"), pd.DataFrame)


def test_pull_config_frames_wins(project, fake_mssql):
    (project / "wh.yaml").write_text(
        (project / "wh.yaml").read_text().replace(
            "destination:", "defaults:\n  frames: pyarrow\ndestination:"
        )
    )
    ws = Workspace.load(project / "wh.yaml")
    assert isinstance(ws.pull("SELECT 1"), pa.Table)


def test_pull_unknown_source(project, fake_mssql):
    ws = Workspace.load(project / "wh.yaml")
    with pytest.raises(ConfigError, match="nope.*warehouse"):
        ws.pull("SELECT 1", source="nope")


def test_land_creates_table(project, fake_mssql):
    ws = Workspace.load(project / "wh.yaml")
    rows = ws.land("SELECT 1", table="scratch.raw")
    assert rows == 3
    assert ws.con.execute('SELECT count(*) FROM "scratch"."raw"').fetchone() == (3,)


def test_land_default_schema_and_replace(project, fake_mssql):
    ws = Workspace.load(project / "wh.yaml")
    ws.land("SELECT 1", table="raw")
    ws.land("SELECT 2", table="raw")         # re-land replaces
    assert ws.con.execute('SELECT count(*) FROM "main"."raw"').fetchone() == (3,)


def test_land_bad_identifier(project, fake_mssql):
    ws = Workspace.load(project / "wh.yaml")
    with pytest.raises(WhError, match="table"):
        ws.land("SELECT 1", table="a.b.c")


def test_module_level_pull(project, fake_mssql, monkeypatch):
    monkeypatch.chdir(project)
    monkeypatch.setattr(wh, "_default", None)
    assert wh.pull("SELECT 1")["n"].to_list() == [1, 2, 3]
