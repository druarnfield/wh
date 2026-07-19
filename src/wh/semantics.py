"""BSL semantic-layer integration: merge YAML, bind to the mirror, load.

The YAML files are the (working-assumption) stable interface; this module
absorbs BSL 0.x API churn. wh owns NO query semantics — see
docs/plans/2026-07-19-semantics-design.md.
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
            f"model table '{ref}' not found in the mirror (available: {available})"
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
