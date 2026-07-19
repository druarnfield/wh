"""BSL semantic-layer integration: merge YAML, bind to the mirror, load.

The YAML files are the (working-assumption) stable interface; this module
absorbs BSL 0.x API churn. wh owns NO query semantics — see
docs/plans/2026-07-19-semantics-design.md.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .errors import SemanticsError

_SIMPLE_COL = re.compile(r"_\.(\w+)")


def _lint_case_only_renames(fname: str, model: str, spec: dict) -> None:
    """Verified upstream bug (BSL 0.3.15 / ibis 12): a dimension or measure
    whose name equals its source column EXCEPT for case breaks execution
    with obscure schema errors. Refuse at load with a real message."""
    for section in ("dimensions", "measures"):
        for name, item in (spec.get(section) or {}).items():
            expr = item.get("expr") if isinstance(item, dict) else item
            m = _SIMPLE_COL.fullmatch(str(expr or "").strip())
            if m and m.group(1) != name and m.group(1).lower() == str(name).lower():
                raise SemanticsError(
                    f"{fname}: model '{model}': '{name}' renames column "
                    f"'{m.group(1)}' only by case — this breaks query execution "
                    f"upstream. Use the exact column case ('{m.group(1)}') or a "
                    f"genuinely different name."
                )


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
            if (
                not isinstance(spec, dict)
                or not spec.get("table")
                or not isinstance(spec.get("table"), str)
            ):
                raise SemanticsError(
                    f"{f.name}: model '{name}' needs a 'table:' key (a string)"
                )
            _lint_case_only_renames(f.name, name, spec)
            merged[name] = spec
            origins[name] = f.name
    return merged, origins


def _import_bsl():
    try:
        import boring_semantic_layer as bsl
        import ibis
    except ImportError as e:
        if e.name in ("boring_semantic_layer", "ibis"):
            raise SemanticsError(
                "semantic models need the semantics extra — "
                "uv add 'warehouse-tools[semantics]'"
            ) from e
        raise SemanticsError(
            f"semantics import failed ({e}) — the [semantics] extra may be broken"
        ) from e
    return bsl, ibis


def _resolve_table(backend, ref: str):
    from .errors import WhError
    from .workspace import _split_table

    try:
        schema, name = _split_table(ref)
    except WhError as e:
        raise SemanticsError(f"invalid table reference '{ref}': {e}") from e
    try:
        return backend.table(name, database=schema)
    except Exception as e:
        rows = backend.con.execute(
            "SELECT schema_name || '.' || table_name FROM duckdb_tables() "
            "WHERE schema_name NOT IN ('_mirror') ORDER BY 1"
        ).fetchall()
        available = ", ".join(r[0] for r in rows) or "none"
        raise SemanticsError(
            f"model table '{ref}' not found in the mirror (available: {available})"
        ) from e


def _check_name_collisions(origin: str, model: str, spec: dict, columns) -> None:
    """Bind-time superset of the YAML lint: ANY declared dim/measure name
    that collides case-insensitively with a table column (without matching
    it exactly) breaks BSL/ibis execution — verified upstream bug."""
    by_lower = {}
    for c in columns:
        by_lower.setdefault(c.lower(), c)
    for section in ("dimensions", "measures"):
        for name in (spec.get(section) or {}):
            col = by_lower.get(str(name).lower())
            if col is not None and col != name:
                raise SemanticsError(
                    f"{origin}: model '{model}': '{name}' collides with column "
                    f"'{col}' only by case — this breaks query execution "
                    f"upstream. Use the exact column case ('{col}') or a "
                    f"genuinely different name."
                )


def validate_semantics(cfg) -> str | None:
    """Validate model files for `wh validate`. Returns a summary line, or
    None when there is nothing to check. Raises SemanticsError on problems.

    Policy: no dir / no model files → skip silently. Files present but the
    [semantics] extra missing → FAIL (silently skipping would let broken
    models pass CI). Full table binding only when the mirror file exists —
    CI typically has none, but structure/duplicates must still fail there."""
    d = cfg.semantics_dir
    if d is None or not d.is_dir():
        return None
    merged, _ = merge_model_files(d)     # structural: parse, duplicates, table keys
    if not merged:
        return None
    _import_bsl()                        # extra is required from here on
    n = len(merged)
    plural = "s" if n != 1 else ""
    if not cfg.duckdb_path.exists():
        return f"OK: {n} semantic model{plural} (structure only — no mirror to bind)"
    import duckdb

    from .workspace import Workspace

    ws = Workspace(cfg)
    try:
        models = ws.models(reload=True)  # full bind
    except duckdb.Error as e:
        raise SemanticsError(
            f"could not open the mirror to bind models: {e} — close open "
            f"notebook sessions holding {cfg.duckdb_path} and retry"
        ) from e
    finally:
        ws.close()
    return f"OK: {len(models)} semantic model{plural} bound"


def load_models(directory: Path, backend) -> dict:
    """Merge all model files and bind them in ONE from_config call."""
    bsl, _ibis = _import_bsl()
    merged, origins = merge_model_files(directory)
    if not merged:
        return {}
    tables: dict = {}
    for mname, spec in merged.items():
        ref = spec["table"]
        if ref not in tables:
            try:
                tables[ref] = _resolve_table(backend, ref)
            except SemanticsError as e:
                raise SemanticsError(f"{origins[mname]}: model '{mname}': {e}") from e
        _check_name_collisions(origins[mname], mname, spec, tables[ref].columns)
    return dict(bsl.from_config(merged, tables=tables))
