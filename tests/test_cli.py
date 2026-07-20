import pytest

from wh.cli import main


def test_validate_ok(project, capsys):
    assert main(["validate", "--config", str(project / "wh.yaml")]) == 0
    assert "OK: 1 tables" in capsys.readouterr().out


def test_validate_bad_config(tmp_path, capsys):
    (tmp_path / "wh.yaml").write_text("sources: {}")
    assert main(["validate", "--config", str(tmp_path / "wh.yaml")]) == 2
    assert "error:" in capsys.readouterr().err


def test_no_config_found(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["validate"]) == 2


def test_mirror_cli_wiring(project, monkeypatch, capsys):
    import duckdb
    import pyarrow as pa
    import wh.sources.mssql as mssql_mod

    class FakeExtractor:
        def __init__(self, source):
            pass

        def __call__(self, spec):
            return pa.table({"a": [1]})

        def close(self):
            pass

    monkeypatch.setattr(mssql_mod, "MssqlExtractor", FakeExtractor)
    assert main(["mirror", "--config", str(project / "wh.yaml")]) == 0

    con = duckdb.connect(str(project / "metrics.duckdb"), read_only=True)
    try:
        assert con.execute('SELECT a FROM "main"."t1"').fetchall() == [(1,)]
    finally:
        con.close()
