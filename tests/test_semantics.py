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


def test_merge_rejects_non_string_table(tmp_path):
    d = write_models(tmp_path / "s", {"a.yml": "m:\n  table: 2024\n"})
    with pytest.raises(SemanticsError, match="table"):
        merge_model_files(d)


def test_bind_rejects_case_colliding_names(semantic_project):
    # broader than the YAML lint: ANY dim/measure name that collides
    # case-insensitively with a table column breaks execution upstream,
    # even for computed exprs — caught at bind time with columns in hand
    from wh.workspace import Workspace

    (semantic_project / "semantics" / "computed.yml").write_text(
        "computed:\n"
        "  table: waitlist\n"
        "  dimensions:\n"
        "    SPECIALTY: _.specialty.upper()\n"     # computed; name collides
        "  measures:\n"
        "    n: _.count()\n"
    )
    ws = Workspace.load(semantic_project / "wh.yaml")
    with pytest.raises(SemanticsError, match="only by case"):
        ws.models()


def test_validate_mirror_locked_is_friendly(semantic_project, capsys):
    import duckdb

    from wh.cli import main

    # read-only handle in-process forces the rw bind connection to fail with
    # duckdb's mixed-configuration ConnectionException — same class of raw
    # error as a cross-process file lock
    blocker = duckdb.connect(str(semantic_project / "metrics.duckdb"), read_only=True)
    try:
        assert main(["validate", "--config", str(semantic_project / "wh.yaml")]) == 2
        assert "error:" in capsys.readouterr().err
    finally:
        blocker.close()


def test_missing_extra_real_import_error(semantic_project, monkeypatch):
    import sys

    from wh.workspace import Workspace

    monkeypatch.setitem(sys.modules, "boring_semantic_layer", None)
    ws = Workspace.load(semantic_project / "wh.yaml")
    with pytest.raises(SemanticsError, match=r"warehouse-tools\[semantics\]"):
        ws.models()


def test_module_level_semantics_verbs(semantic_project, monkeypatch):
    import polars as pl

    import wh

    monkeypatch.chdir(semantic_project)
    monkeypatch.setattr(wh, "_default", None)
    assert set(wh.models()) == {"waitlist", "clinics"}
    q = wh.model("waitlist").group_by("specialty").aggregate("patients_waiting")
    assert isinstance(wh.frame(q), pl.DataFrame)


def test_merge_rejects_case_only_renames(tmp_path):
    # verified upstream bug (BSL 0.3.15/ibis 12): a dim named 'specialty'
    # over column _.Specialty breaks both to_pyarrow() and execute() with
    # obscure schema errors — catch it at load with a real message
    d = write_models(tmp_path / "s", {
        "a.yml": (
            "m:\n  table: t\n"
            "  dimensions:\n    specialty: _.Specialty\n"
        ),
    })
    with pytest.raises(SemanticsError, match="only by case"):
        merge_model_files(d)


def test_merge_allows_exact_case_and_real_renames(tmp_path):
    d = write_models(tmp_path / "s", {
        "a.yml": (
            "m:\n  table: t\n"
            "  dimensions:\n"
            "    Specialty: _.Specialty\n"          # exact case: fine
            "    clinical_spec:\n"
            "      expr: _.Specialty\n"             # real rename: fine
        ),
    })
    merged, _ = merge_model_files(d)
    assert "m" in merged


def test_models_bind_and_query(semantic_project):
    from wh.workspace import Workspace

    ws = Workspace.load(semantic_project / "wh.yaml")
    models = ws.models()
    assert set(models) == {"waitlist", "clinics"}
    wl = ws.model("waitlist")
    out = wl.group_by("specialty").aggregate("patients_waiting").execute()
    assert sorted(out.values.tolist()) == [["Cardio", 2], ["Ortho", 1]]


def test_model_unknown_lists_available(semantic_project):
    from wh.workspace import Workspace

    ws = Workspace.load(semantic_project / "wh.yaml")
    with pytest.raises(SemanticsError, match="clinics, waitlist"):
        ws.model("nope")


def test_dotted_schema_binding(semantic_project):
    from wh.workspace import Workspace

    ws = Workspace.load(semantic_project / "wh.yaml")
    out = ws.model("clinics").aggregate("n_clinics").execute()
    assert out.values.tolist() == [[2]]


JOINED_MODEL = (
    "wl_regional:\n"
    "  table: waitlist\n"
    "  dimensions:\n"
    "    clinic:\n"
    "      expr: _.clinic_code\n"
    "      is_entity: true\n"
    "  measures:\n"
    "    patients: _.count()\n"
    "  joins:\n"
    "    clinics:\n"
    "      model: clinics\n"          # defined in clinics.yml — a DIFFERENT file
    "      type: one\n"
    "      left_on: clinic\n"
    "      right_on: code\n"
)


@pytest.mark.filterwarnings("ignore:Grain mismatch detected")
def test_cross_file_join_models_load(semantic_project):
    # merge-then-one-call exists precisely so this does not KeyError:
    # per-file from_yaml loading cannot resolve joins across files.
    # NOTE: only LOADING is asserted — in BSL 0.3.15 a declared join breaks
    # every query on that model (see xfail below), so joins are load-safe
    # but not yet usable upstream.
    from wh.workspace import Workspace

    (semantic_project / "semantics" / "joined.yml").write_text(JOINED_MODEL)
    ws = Workspace.load(semantic_project / "wh.yaml")
    assert "wl_regional" in ws.models()
    ws.model("wl_regional")     # lookup succeeds; no KeyError from cross-file ref


@pytest.mark.xfail(
    strict=True,
    reason="BSL 0.3.15 join querying is broken (grain-mismatch heuristic → "
    "'No aggregation results and full join unavailable') regardless of file "
    "layout; strict so we notice the release that fixes it",
)
@pytest.mark.filterwarnings("ignore:Grain mismatch detected")
def test_join_dimension_query(semantic_project):
    from wh.workspace import Workspace

    (semantic_project / "semantics" / "joined.yml").write_text(JOINED_MODEL)
    ws = Workspace.load(semantic_project / "wh.yaml")
    out = (
        ws.model("wl_regional")
        .group_by("clinics.region")
        .aggregate("patients")
        .execute()
    )
    assert sorted(out.values.tolist()) == [["North", 2], ["South", 1]]


def test_unresolvable_table_lists_candidates(semantic_project):
    from wh.workspace import Workspace

    (semantic_project / "semantics" / "bad.yml").write_text(
        "bad:\n  table: no_such\n  measures:\n    n: _.count()\n"
    )
    ws = Workspace.load(semantic_project / "wh.yaml")
    with pytest.raises(SemanticsError, match="waitlist"):
        ws.models()


def test_no_semantics_dir_is_empty(project):
    from wh.workspace import Workspace

    assert Workspace.load(project / "wh.yaml").models() == {}


def test_missing_extra_message(semantic_project, monkeypatch):
    import wh.semantics as sem
    from wh.workspace import Workspace

    def no_bsl():
        raise SemanticsError(
            "semantic models need the semantics extra — "
            "uv add 'warehouse-tools[semantics]'"
        )

    monkeypatch.setattr(sem, "_import_bsl", no_bsl)
    ws = Workspace.load(semantic_project / "wh.yaml")
    with pytest.raises(SemanticsError, match=r"warehouse-tools\[semantics\]"):
        ws.models()


def test_cache_self_keys_on_connection(semantic_project):
    from wh.workspace import Workspace

    ws = Workspace.load(semantic_project / "wh.yaml")
    m1 = ws.models()
    assert ws.models() is m1                 # cached
    ws.close()                               # ANY reopen path, not just mirror()
    m2 = ws.models()
    assert m2 is not m1                      # rebuilt on the new connection
    assert set(m2) == set(m1)


def test_register_then_reload_binds_frame(semantic_project):
    import polars as pl

    from wh.workspace import Workspace

    ws = Workspace.load(semantic_project / "wh.yaml")
    ws.register(pl.DataFrame({"ur": ["U1", "U2"]}), "cohort")
    (semantic_project / "semantics" / "cohort.yml").write_text(
        "cohort:\n  table: cohort\n  measures:\n    n: _.count()\n"
    )
    out = ws.models(reload=True)["cohort"].aggregate("n").execute()
    assert out.values.tolist() == [[2]]


def test_frame_converts_bsl_query(semantic_project):
    import polars as pl

    from wh.workspace import Workspace

    ws = Workspace.load(semantic_project / "wh.yaml")
    q = ws.model("waitlist").group_by("specialty").aggregate("patients_waiting")
    df = ws.frame(q)
    assert isinstance(df, pl.DataFrame)
    assert sorted(df.rows()) == [("Cardio", 2), ("Ortho", 1)]


def test_frame_backend_override_and_generic(semantic_project):
    import pandas as pd
    import polars as pl

    from wh.workspace import Workspace

    ws = Workspace.load(semantic_project / "wh.yaml")
    q = ws.model("waitlist").aggregate("patients_waiting")
    assert isinstance(ws.frame(q, backend="pandas"), pd.DataFrame)
    assert isinstance(ws.frame(pd.DataFrame({"a": [1]})), pl.DataFrame)  # generic


def test_models_reload_picks_up_edits(semantic_project):
    from wh.workspace import Workspace

    ws = Workspace.load(semantic_project / "wh.yaml")
    assert "extra" not in ws.models()
    (semantic_project / "semantics" / "extra.yml").write_text(
        "extra:\n  table: waitlist\n  measures:\n    n: _.count()\n"
    )
    assert "extra" not in ws.models()            # still cached
    assert "extra" in ws.models(reload=True)
