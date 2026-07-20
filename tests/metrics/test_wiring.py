"""End-to-end wiring: Workspace.model()/slice(), module verbs, mtime reload."""

import os

import duckdb
import pytest

import wh
from wh.errors import SemanticsError
from wh.workspace import Workspace

from fixtures_data import DIMS_YAML, REMOVALS_YAML, SEED_SQL, WAITLIST_YAML


@pytest.fixture
def metric_project(project):
    """The root `project` fixture plus a seeded .duckdb and semantics/."""
    c = duckdb.connect(str(project / "metrics.duckdb"))
    c.execute(SEED_SQL)
    c.close()
    sdir = project / "semantics"
    sdir.mkdir()
    (sdir / "dims.yml").write_text(DIMS_YAML)
    (sdir / "waitlist.yml").write_text(WAITLIST_YAML)
    (sdir / "removals.yml").write_text(REMOVALS_YAML)
    cfg = project / "wh.yaml"
    cfg.write_text(cfg.read_text() + "semantics:\n  fiscal_year_start: 7\n")
    return project


def test_workspace_slice_end_to_end(metric_project):
    ws = Workspace.load(metric_project / "wh.yaml")
    s = ws.slice(
        "waitlist",
        measures=["patients_waiting"],
        context=wh.context(time=("2026-06-01", "2026-06-30")),
    )
    t = s.frame(backend="pyarrow")
    assert t.column("patients_waiting").to_pylist() == [4]
    ws.close()


def test_unknown_model_names_the_available(metric_project):
    ws = Workspace.load(metric_project / "wh.yaml")
    with pytest.raises(SemanticsError, match="waitlist"):
        ws.model("nope")
    ws.close()


def test_yaml_mtime_bump_reloads_definitions(metric_project):
    ws = Workspace.load(metric_project / "wh.yaml")
    before = ws.model("removals").slice(measures=["removals"]).frame(backend="pyarrow")
    assert before.column("removals").to_pylist() == [4]

    path = metric_project / "semantics" / "removals.yml"
    path.write_text(REMOVALS_YAML.replace("<> 'ADMIN'", "= 'ADMIN'"))
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))

    after = ws.model("removals").slice(measures=["removals"]).frame(backend="pyarrow")
    assert after.column("removals").to_pylist() == [2]
    ws.close()


def test_module_verbs_exist():
    assert callable(wh.model) and callable(wh.slice) and callable(wh.context)
    assert callable(wh.not_) and callable(wh.last) and callable(wh.all)
