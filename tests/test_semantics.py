from pathlib import Path

import pytest

from wh.errors import SemanticsError
from wh.semantics import merge_model_files


def write_models(d: Path, files: dict[str, str]):
    d.mkdir(exist_ok=True)
    for name, text in files.items():
        (d / name).write_text(text)
    return d


def test_merge_scans_yml_and_yaml_sorted(tmp_path):
    d = write_models(tmp_path / "s", {
        "b.yaml": "m2:\n  table: t2\n",
        "a.yml": "m1:\n  table: t1\n",
    })
    merged, origins = merge_model_files(d)
    assert list(merged) == ["m1", "m2"]          # a.yml before b.yaml
    assert origins == {"m1": "a.yml", "m2": "b.yaml"}


def test_merge_duplicate_names_both_files(tmp_path):
    d = write_models(tmp_path / "s", {
        "a.yml": "m:\n  table: t\n",
        "b.yml": "m:\n  table: t\n",
    })
    with pytest.raises(SemanticsError, match=r"a\.yml.*b\.yml"):
        merge_model_files(d)


def test_merge_rejects_non_mapping(tmp_path):
    d = write_models(tmp_path / "s", {"a.yml": "- just\n- a list\n"})
    with pytest.raises(SemanticsError, match="mapping"):
        merge_model_files(d)


def test_merge_missing_table_key(tmp_path):
    d = write_models(tmp_path / "s", {"a.yml": "m:\n  dimensions: {}\n"})
    with pytest.raises(SemanticsError, match="table"):
        merge_model_files(d)


def test_merge_empty_dir(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    assert merge_model_files(d) == ({}, {})
