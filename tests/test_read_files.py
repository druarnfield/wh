import polars as pl
import pytest

import wh
from wh.workspace import Workspace


def test_workspace_read_excel_backend(project, messy_xlsx):
    ws = Workspace.load(project / "wh.yaml")
    df = ws.read_excel(messy_xlsx)
    assert isinstance(df, pl.DataFrame)
    assert df["UR"].to_list() == ["A1", "A2"]


def test_workspace_read_excel_lands(project, messy_xlsx):
    ws = Workspace.load(project / "wh.yaml")
    assert ws.read_excel(messy_xlsx, land="files.waitlist") == 2
    assert ws.con.execute('SELECT count(*) FROM "files"."waitlist"').fetchone() == (2,)


def test_workspace_read_csv(project, tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("a,b\n1,x\n2,y\n")
    ws = Workspace.load(project / "wh.yaml")
    df = ws.read_csv(p)
    assert isinstance(df, pl.DataFrame)
    assert df["a"].to_list() == [1, 2]


def test_workspace_read_csv_lands(project, tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("a,b\n1,x\n2,y\n")
    ws = Workspace.load(project / "wh.yaml")
    assert ws.read_csv(p, land="files.d") == 2
    assert ws.con.execute('SELECT sum(a) FROM "files"."d"').fetchone() == (3,)


def test_module_read_excel_works_without_config(messy_xlsx, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)               # no wh.yaml anywhere above tmp
    monkeypatch.setattr(wh, "_default", None)
    df = wh.read_excel(messy_xlsx)
    assert df["UR"].to_list() == ["A1", "A2"]


def test_module_read_csv_works_without_config(tmp_path, monkeypatch):
    p = tmp_path / "d.csv"
    p.write_text("a\n1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wh, "_default", None)
    assert wh.read_csv(p)["a"].to_list() == [1]


def test_module_land_without_config_raises(messy_xlsx, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wh, "_default", None)
    with pytest.raises(wh.ConfigError):
        wh.read_excel(messy_xlsx, land="files.x")


def test_clean_exported():
    assert callable(wh.clean) and callable(wh.clean.snake_names)


def test_module_reader_propagates_broken_config(messy_xlsx, tmp_path, monkeypatch):
    # a BROKEN wh.yaml must surface, not silently fall back to configless mode
    (tmp_path / "wh.yaml").write_text("sources: [unclosed")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wh, "_default", None)
    with pytest.raises(wh.ConfigError, match="invalid YAML"):
        wh.read_excel(messy_xlsx)


def test_module_reader_explicit_land_none_configless(messy_xlsx, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wh, "_default", None)
    df = wh.read_excel(messy_xlsx, land=None)     # explicit None = frame path
    assert df["UR"].to_list() == ["A1", "A2"]


def test_workspace_read_csv_frame_path_leaves_no_db(project, tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("a\n1\n")
    ws = Workspace.load(project / "wh.yaml")
    ws.read_csv(p)
    assert not (project / "metrics.duckdb").exists()   # sniffing is in-memory


def test_read_csv_missing_file_is_friendly(project):
    ws = Workspace.load(project / "wh.yaml")
    with pytest.raises(wh.WhError, match="no such file"):
        ws.read_csv(project / "nope.csv")
