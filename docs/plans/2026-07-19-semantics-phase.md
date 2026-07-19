# warehouse-tools Semantics Phase Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** BSL semantic layer per `docs/plans/2026-07-19-semantics-design.md`: `wh.models()` / `wh.model()` bound to the mirror, generic `wh.frame()`, `wh validate` semantics check. Zero query semantics in wh.

**Architecture:** `src/wh/semantics.py` holds a pure merge step (`merge_model_files` — testable without BSL), the lazy-import seam (`_import_bsl`), and the bind step (single `bsl.from_config(merged, tables=...)` call; tables resolved by splitting schema/name and calling `con.table(name, database=schema)`). `Workspace` gains `_ibis()`, `models()`, `model()`, `frame()` with **self-keying caches** (keyed on connection/backend object identity — no mirror hook). `frames.to_arrow` learns a `.to_pyarrow()` duck-type, which is all `frame()` needs. Validate runs structural checks always, full bind checks only when the mirror file exists (CI has no mirror; structure/duplicates must still fail there).

**Tech Stack:** boring-semantic-layer >=0.3.15,<0.4 + ibis-framework[duckdb] (verified probes: `scratchpad/bsl/probe*.py` — join syntax is `joins: {name: {model:, type: one, left_on:, right_on:}}` with `is_entity: true` dims).

**Conventions:** TDD; `set -o pipefail` before pytest→git chains; no Claude in commits; no submodule/verb collisions (module `semantics`, verbs `models/model/frame`).

---

## Task 1: Deps, error type, config key

**Step 1:** pyproject: add to `[project.optional-dependencies]`:

```toml
semantics = [
    "boring-semantic-layer>=0.3.15,<0.4",
    "ibis-framework[duckdb]>=10",
]
```

Then `uv add --group dev "boring-semantic-layer>=0.3.15,<0.4" "ibis-framework[duckdb]"` (resolution failure here = ibis/duckdb pin conflict; resolve before proceeding).

**Step 2:** Smoke-probe the installed version (API may drift within 0.3.x):

```bash
uv run python -c "import boring_semantic_layer as b; print(b.from_config, b.from_yaml)"
```

Expected: both print. If `from_config` is missing, STOP and revisit the design's merge-then-one-call decision.

**Step 3 (TDD):** `tests/test_errors.py`: add `SemanticsError` to the inheritance test's tuple. Run (fails) → add `class SemanticsError(WhError)` to `src/wh/errors.py`, export from `src/wh/__init__.py` (`__all__` + import). Run → pass.

**Step 4 (TDD):** `tests/test_config_load.py`:

```python
def test_semantics_dir_default(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert cfg.semantics_dir == tmp_path / "semantics"


def test_semantics_dir_custom(tmp_path):
    d = {**VALID, "semantics": {"dir": "./defs"}}
    assert load_config(write(tmp_path, d)).semantics_dir == tmp_path / "defs"
```

Implement: `Config.semantics_dir: Path` field; in `load_config`:
`semantics_dir=(base / ((raw.get("semantics") or {}).get("dir", "semantics"))).resolve()`.

**Step 5:** Full suite → pass. Commit: `feat: semantics extra, SemanticsError, semantics.dir config`

---

## Task 2: Pure merge step

**Files:** Create `src/wh/semantics.py`; test `tests/test_semantics.py`.

**Step 1: Failing tests** (no BSL import needed — pure YAML merging):

```python
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
```

**Step 2:** Run → collection error. **Step 3: Implement `src/wh/semantics.py`** (top half):

```python
"""BSL semantic-layer integration: merge YAML, bind to the mirror, load.

The YAML files are the (working-assumption) stable interface; this module
absorbs BSL 0.x API churn. wh owns NO query semantics — see the design doc.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .errors import SemanticsError


def merge_model_files(directory: Path) -> tuple[dict, dict]:
    """Merge semantics/*.yml|*.yaml into one config dict.

    One merged dict → one bsl.from_config call, so joins work across files
    (BSL only resolves joins within a single load — verified).
    Returns (merged_config, {model_name: filename})."""
    merged: dict = {}
    origins: dict[str, str] = {}
    files = sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")])
    for f in files:
        try:
            raw = yaml.safe_load(f.read_text())
        except yaml.YAMLError as e:
            raise SemanticsError(f"{f.name}: invalid YAML: {e}") from e
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise SemanticsError(f"{f.name}: root must be a mapping of model names")
        for name, spec in raw.items():
            if name in origins:
                raise SemanticsError(
                    f"model '{name}' defined in both {origins[name]} and {f.name}"
                )
            if not isinstance(spec, dict) or not spec.get("table"):
                raise SemanticsError(
                    f"{f.name}: model '{name}' needs a 'table:' key"
                )
            merged[name] = spec
            origins[name] = f.name
    return merged, origins
```

**Step 4:** Pass. Commit: `feat: semantics YAML merge (cross-file, duplicate detection)`

---

## Task 3: Import seam, binding, `models()`/`model()` with self-keying caches

**Files:** Modify `src/wh/semantics.py`, `src/wh/workspace.py`, `src/wh/__init__.py`; test `tests/test_semantics.py` (+ fixture in conftest).

**Step 1: conftest fixture** (mini mirror via existing helpers):

```python
@pytest.fixture
def semantic_project(project):
    """project + a built mini-mirror (waitlist, files.clinics) + model YAML."""
    import pyarrow as pa

    from wh.mirror import build
    from wh.config import load_config

    cfg = load_config(project / "wh.yaml")
    build(
        cfg._replace_tables_hack__see_below if False else cfg,  # (see step note)
        ...,
    )
    return project
```

Plan note (implementer): don't fight the fixture — build the mirror directly:

```python
@pytest.fixture
def semantic_project(project):
    import duckdb

    con = duckdb.connect(str(project / "metrics.duckdb"))
    con.execute("CREATE TABLE waitlist AS SELECT * FROM (VALUES "
                "('U1','Cardio','C1',40),('U2','Cardio','C2',10),('U3','Ortho','C1',60)"
                ") t(patient_ur, specialty, clinic_code, wait_days)")
    con.execute("CREATE SCHEMA files")
    con.execute("CREATE TABLE files.clinics AS SELECT * FROM (VALUES "
                "('C1','North'),('C2','South')) t(code, region)")
    con.close()
    sdir = project / "semantics"
    sdir.mkdir()
    (sdir / "waitlist.yml").write_text(
        "waitlist:\n"
        "  table: waitlist\n"
        "  dimensions:\n"
        "    specialty: _.specialty\n"
        "    clinic:\n"
        "      expr: _.clinic_code\n"
        "      is_entity: true\n"
        "  measures:\n"
        "    patients_waiting: _.patient_ur.nunique()\n"
        "    median_wait_days: _.wait_days.median()\n"
    )
    (sdir / "clinics.yml").write_text(
        "clinics:\n"
        "  table: files.clinics\n"
        "  dimensions:\n"
        "    code:\n"
        "      expr: _.code\n"
        "      is_entity: true\n"
        "    region: _.region\n"
        "  measures:\n"
        "    n_clinics: _.count()\n"
    )
    return project
```

**Step 2: Failing tests**

```python
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


def test_cross_file_join(semantic_project):
    from wh.workspace import Workspace

    (semantic_project / "semantics" / "joined.yml").write_text(
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
        "      model: clinics\n"          # defined in clinics.yml — other file
        "      type: one\n"
        "      left_on: clinic\n"
        "      right_on: code\n"
    )
    ws = Workspace.load(semantic_project / "wh.yaml")
    out = ws.model("wl_regional").group_by("clinics.region").aggregate("patients").execute()
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
        raise SemanticsError("semantic models need the semantics extra — "
                             "uv add 'warehouse-tools[semantics]'")

    monkeypatch.setattr(sem, "_import_bsl", no_bsl)
    ws = Workspace.load(semantic_project / "wh.yaml")
    with pytest.raises(SemanticsError, match="warehouse-tools\\[semantics\\]"):
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


def test_models_reload_picks_up_edits(semantic_project):
    from wh.workspace import Workspace

    ws = Workspace.load(semantic_project / "wh.yaml")
    assert "extra" not in ws.models()
    (semantic_project / "semantics" / "extra.yml").write_text(
        "extra:\n  table: waitlist\n  measures:\n    n: _.count()\n"
    )
    assert "extra" not in ws.models()            # still cached
    assert "extra" in ws.models(reload=True)
```

**Step 3:** Run → failures. **Step 4: Implement.**

`semantics.py` (bottom half):

```python
def _import_bsl():
    try:
        import boring_semantic_layer as bsl
        import ibis
    except ImportError as e:
        raise SemanticsError(
            "semantic models need the semantics extra — "
            "uv add 'warehouse-tools[semantics]'"
        ) from e
    return bsl, ibis


def _resolve_table(backend, ref: str):
    from .workspace import _split_table

    schema, name = _split_table(ref)
    try:
        return backend.table(name, database=schema)
    except Exception as e:
        rows = backend.con.execute(
            "SELECT schema_name || '.' || table_name FROM duckdb_tables() "
            "WHERE schema_name NOT IN ('_mirror') ORDER BY 1"
        ).fetchall()
        available = ", ".join(r[0] for r in rows) or "none"
        raise SemanticsError(
            f"model table '{ref}' not found in the mirror "
            f"(available: {available})"
        ) from e


def load_models(directory: Path, backend) -> dict:
    """Merge all model files and bind them in ONE from_config call."""
    bsl, _ibis = _import_bsl()
    merged, _origins = merge_model_files(directory)
    if not merged:
        return {}
    tables = {
        spec["table"]: _resolve_table(backend, spec["table"])
        for spec in merged.values()
    }
    return dict(bsl.from_config(merged, tables=tables))
```

`workspace.py`: in `__init__` add `self._ibis_cache = None` and `self._models_cache = None`; add methods:

```python
    def _ibis(self):
        """Ibis backend over the session connection. Self-keying cache:
        rebuilt whenever the underlying connection object changed (mirror
        swap, manual close(), death) — no invalidation hooks anywhere."""
        from .semantics import _import_bsl

        con = self.con
        if self._ibis_cache is None or self._ibis_cache[0] is not con:
            _bsl, ibis = _import_bsl()
            self._ibis_cache = (con, ibis.duckdb.from_connection(con))
        return self._ibis_cache[1]

    def models(self, reload: bool = False) -> dict:
        """All semantic models, bound to the mirror. reload=True re-reads YAML."""
        from .semantics import load_models

        backend = self._ibis()
        if reload or self._models_cache is None or self._models_cache[0] is not backend:
            d = self.config.semantics_dir
            loaded = load_models(d, backend) if d.is_dir() else {}
            self._models_cache = (backend, loaded)
        return self._models_cache[1]

    def model(self, name: str):
        """One semantic model by name; the fluent BSL API hangs off it."""
        from .errors import SemanticsError

        models = self.models()
        if name not in models:
            available = ", ".join(sorted(models)) or (
                "none — add YAML files to " + str(self.config.semantics_dir)
            )
            raise SemanticsError(f"no semantic model '{name}' (available: {available})")
        return models[name]
```

Ordering note: `models()` must call `self._ibis()` BEFORE checking the cache key (as written) so a dead connection heals first. `_import_bsl` is called in `_ibis` (needs ibis) and again harmlessly in `load_models`.

`__init__.py`: add `models`, `model` wrappers + `__all__` entries (no eager submodule import needed — no name collision).

**Step 5:** Full suite → pass (the join test may need `is_entity` adjustments; probe files in `scratchpad/bsl/` are the reference). Commit: `feat: semantic models — merged loading, mirror binding, self-keying caches`

---

## Task 4: `frame()` + `to_arrow` duck-type

**Step 1 (TDD):** `tests/test_frames.py`:

```python
def test_to_arrow_ducktypes_to_pyarrow():
    class FakeExpr:
        def to_pyarrow(self):
            return TABLE

    assert to_arrow(FakeExpr()) is TABLE
```

`tests/test_semantics.py`:

```python
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
```

**Step 2:** Run → fail. **Step 3: Implement.** In `frames.to_arrow`, after the `DuckDBPyRelation` branch:

```python
    if hasattr(obj, "to_pyarrow"):          # ibis expressions, BSL queries
        return obj.to_pyarrow()
```

In `workspace.py`:

```python
    def frame(self, obj, backend: str | None = None):
        """Convert anything frame-ish (BSL query, ibis expr, pandas/polars,
        DuckDB relation, Arrow) to the preferred backend."""
        from .frames import from_arrow, to_arrow

        return from_arrow(to_arrow(obj), self._backend(backend))
```

Module wrapper `wh.frame` + `__all__`. **Step 4:** Pass. Commit: `feat: frame() — generic conversion incl. ibis/BSL expressions`

---

## Task 5: `wh validate` semantics check

Policy (design + one refinement): no dir / no model files → silent skip. Files present + extra missing → FAIL with install command. Files present + extra installed: structural checks always; **full bind check only if the mirror .duckdb exists** (CI has no mirror — table resolution there is impossible, but structure/duplicates must still fail). Report which level ran.

**Step 1 (TDD):** `tests/test_cli.py`:

```python
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
    assert "1 semantic models" in out and "structure only" in out


def test_validate_semantics_full_bind_with_mirror(semantic_project, capsys):
    (semantic_project / "semantics" / "bad.yml").write_text(
        "bad:\n  table: no_such\n  measures:\n    n: _.count()\n"
    )
    assert main(["validate", "--config", str(semantic_project / "wh.yaml")]) == 2
    assert "no_such" in capsys.readouterr().err
```

**Step 2:** Run → fail. **Step 3: Implement.** In `semantics.py`:

```python
def validate_semantics(cfg) -> str | None:
    """Validate model files. Returns a summary line, or None when there is
    nothing to check. Raises SemanticsError on any problem."""
    d = cfg.semantics_dir
    if not d.is_dir():
        return None
    merged, _ = merge_model_files(d)      # structural: parse, duplicates, table keys
    if not merged:
        return None
    if not cfg.duckdb_path.exists():
        return f"OK: {len(merged)} semantic models (structure only — no mirror to bind)"
    _import_bsl()                          # extra required from here on
    from .workspace import Workspace

    ws = Workspace(cfg)
    try:
        models = ws.models(reload=True)    # full bind
    finally:
        ws.close()
    return f"OK: {len(models)} semantic models bound"
```

Wait — extra-missing must fail even without a mirror when model files exist (design policy). Move `_import_bsl()` up, directly after the `if not merged` check, BEFORE the mirror-existence branch. (The structure-only path still requires the extra to be *installed*; it just doesn't need the mirror. That matches the design: files present + extra missing → fail.)

In `cli.py` validate branch, after the existing print:

```python
            from .semantics import validate_semantics

            summary = validate_semantics(cfg)
            if summary:
                print(summary)
```

(`WhError` handler already catches `SemanticsError` → exit 2.)

**Step 4:** Pass, full suite. Commit: `feat: wh validate checks semantic models (structural always, bind when mirror exists)`

---

## Task 6: Docs, repo example, acceptance

**Step 1:** Add `semantics/waitlist.yml` to THIS repo (dev workspace) over the real mirrored table:

```yaml
waitlist:
  table: outpatient_waitlist_current
  description: "Current outpatient waitlist"
  dimensions:
    specialty: _.Specialty        # adjust to actual column names via wh.connect()
  measures:
    patients_waiting: _.count()   # adjust to a UR-style nunique if a UR col exists
```

(Inspect real columns first: `uv run python -c "import wh; print(wh.connect().execute('DESCRIBE main.outpatient_waitlist_current').fetchall())"`.)

**Step 2: Acceptance** (live):

```python
import wh
wl = wh.model("waitlist")
print(wl)                                        # repr: dims/measures
q = wl.group_by("specialty").aggregate("patients_waiting")
print(q.sql())                                   # generated SQL
print(wh.frame(q))                               # polars frame
```

And `uv run wh validate` → "OK: ... semantic models bound".

**Step 3:** README: semantics section (extra install, wh.yaml key optional, YAML example, model/frame usage, "wh wraps no query API — BSL's fluent API is the query language"). CLAUDE.md: semantics phase COMPLETE + notes (merge-then-one-call rationale, self-keying caches — never add invalidation hooks, `frames.py` module vs `frame` verb naming care, BSL 0.x churn absorbed in semantics.py only, upstream issue re error messages).

**Step 4:** Full suite; commit: `feat: semantics phase — BSL models over the mirror (docs + acceptance)`

---

## Done — acceptance

- `wh.model("waitlist")` + fluent BSL + `wh.frame()` returns correct polars values against the live mirror
- Cross-file joins load; dotted schemas bind; registered frames bind post-reload
- Caches rebuild after `mirror()` and `close()` with no hooks
- `wh validate`: skips silently (no models), fails helpfully (broken/missing extra/unresolvable), binds fully when a mirror exists
- Post-implementation: adversarial code review, then file the BSL upstream issue re raw-column error messages
