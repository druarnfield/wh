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
