import pytest

from wh.config import find_config
from wh.errors import ConfigError


def test_finds_in_cwd(tmp_path, monkeypatch):
    (tmp_path / "wh.yaml").write_text("x: 1")
    monkeypatch.chdir(tmp_path)
    assert find_config() == tmp_path / "wh.yaml"


def test_walks_up(tmp_path, monkeypatch):
    (tmp_path / "wh.yaml").write_text("x: 1")
    deep = tmp_path / "notebooks" / "july"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert find_config() == tmp_path / "wh.yaml"


def test_not_found_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="wh.yaml"):
        find_config()
