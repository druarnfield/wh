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


def test_validate_semantics_structural_failure(project, capsys):
    sdir = project / "semantics"
    sdir.mkdir()
    (sdir / "bad.yml").write_text("- not a mapping\n")
    assert main(["validate", "--config", str(project / "wh.yaml")]) == 2
    assert "bad.yml" in capsys.readouterr().err


def test_validate_semantics_ok_without_mirror(project, capsys):
    sdir = project / "semantics"
    sdir.mkdir()
    (sdir / "m.yml").write_text("m:\n  table: t1\n  measures:\n    n: _.count()\n")
    assert main(["validate", "--config", str(project / "wh.yaml")]) == 0
    out = capsys.readouterr().out
    assert "1 semantic model" in out and "structure only" in out


def test_validate_semantics_full_bind_with_mirror(semantic_project, capsys):
    (semantic_project / "semantics" / "bad.yml").write_text(
        "bad:\n  table: no_such\n  measures:\n    n: _.count()\n"
    )
    assert main(["validate", "--config", str(semantic_project / "wh.yaml")]) == 2
    assert "no_such" in capsys.readouterr().err


def test_validate_no_semantics_dir_still_ok(project, capsys):
    assert main(["validate", "--config", str(project / "wh.yaml")]) == 0
    assert "semantic model" not in capsys.readouterr().out


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
