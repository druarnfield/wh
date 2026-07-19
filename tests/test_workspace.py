import duckdb
import pytest

import wh
from wh.workspace import Workspace


def test_load_explicit_path(project):
    ws = Workspace.load(project / "wh.yaml")
    assert ws.config.duckdb_path == project / "metrics.duckdb"


def test_connect_creates_and_queries(project):
    ws = Workspace.load(project / "wh.yaml")
    con = ws.connect(fresh=True)
    assert con.execute("SELECT 42").fetchone() == (42,)
    con.close()
    assert (project / "metrics.duckdb").exists()


def test_shared_connection_is_cached(project):
    ws = Workspace.load(project / "wh.yaml")
    assert ws.con is ws.con
    assert ws.connect() is ws.con          # default connect() = the session con


def test_connect_fresh_is_independent(project):
    ws = Workspace.load(project / "wh.yaml")
    fresh = ws.connect(fresh=True)
    assert fresh is not ws.con
    fresh.close()


def test_freshness_works_while_rw_connection_open(project):
    # regression: read_only + rw on one file in-process is a DuckDB error
    import pyarrow as pa
    ws = Workspace.load(project / "wh.yaml")
    ws.mirror(extract=lambda spec: pa.table({"a": [1]}), log=lambda s: None)
    con = ws.con                            # hold a live rw connection
    assert ws.freshness().num_rows == 1
    assert con.execute("SELECT 1").fetchone() == (1,)


def test_mirror_reopens_shared_connection(project):
    import pyarrow as pa
    ws = Workspace.load(project / "wh.yaml")
    ws.mirror(extract=lambda spec: pa.table({"a": [1]}), log=lambda s: None)
    _ = ws.con
    ws.mirror(extract=lambda spec: pa.table({"a": [2]}), log=lambda s: None)
    assert ws.con.execute('SELECT a FROM "main"."t1"').fetchall() == [(2,)]


def test_module_level_uses_discovery(project, monkeypatch):
    sub = project / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    monkeypatch.setattr(wh, "_default", None)  # reset the lazy singleton
    con = wh.connect()
    assert con.execute("SELECT 1").fetchone() == (1,)


def test_module_workspace_explicit_path_bypasses_singleton(project):
    ws = wh.workspace(project / "wh.yaml")
    assert isinstance(ws, Workspace)


def test_workspace_mirror_with_injected_extract(project):
    import pyarrow as pa
    ws = Workspace.load(project / "wh.yaml")
    ws.mirror(extract=lambda spec: pa.table({"a": [1]}), log=lambda s: None)
    con = ws.connect(read_only=True)
    assert con.execute('SELECT a FROM "main"."t1"').fetchall() == [(1,)]
    con.close()


def test_freshness(project):
    import pyarrow as pa
    ws = Workspace.load(project / "wh.yaml")
    ws.mirror(extract=lambda spec: pa.table({"a": [1]}), log=lambda s: None)
    fresh = ws.freshness()
    assert fresh.num_rows == 1
    assert "extracted_at" in fresh.column_names


def test_freshness_before_any_mirror_is_friendly(project):
    ws = Workspace.load(project / "wh.yaml")
    with pytest.raises(wh.WhError, match="run wh.mirror"):
        ws.freshness()


def test_freshness_on_empty_db_from_connect_is_friendly(project):
    ws = Workspace.load(project / "wh.yaml")
    ws.connect().close()  # creates an empty .duckdb with no _mirror.meta
    with pytest.raises(wh.WhError, match="run wh.mirror"):
        ws.freshness()
