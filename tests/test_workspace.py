import duckdb

import wh
from wh.workspace import Workspace


def test_load_explicit_path(project):
    ws = Workspace.load(project / "wh.yaml")
    assert ws.config.duckdb_path == project / "metrics.duckdb"


def test_connect_creates_and_queries(project):
    ws = Workspace.load(project / "wh.yaml")
    con = ws.connect()
    assert con.execute("SELECT 42").fetchone() == (42,)
    con.close()
    assert (project / "metrics.duckdb").exists()


def test_module_level_uses_discovery(project, monkeypatch):
    sub = project / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    monkeypatch.setattr(wh, "_default", None)  # reset the lazy singleton
    con = wh.connect()
    assert con.execute("SELECT 1").fetchone() == (1,)
    con.close()


def test_module_workspace_explicit_path_bypasses_singleton(project):
    ws = wh.workspace(project / "wh.yaml")
    assert isinstance(ws, Workspace)
