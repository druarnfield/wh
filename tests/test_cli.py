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
