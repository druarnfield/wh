"""Provenance: content hashing and the object that lets a number explain
itself. Design: docs/plans/2026-07-20-metrics-design.md (Provenance section).

Measure hashes cover a MINIMAL SEMANTIC PROJECTION of the parse tree, not
DuckDB's raw serialization: only stable semantic keys survive (class, type,
function names, column names case-folded, constant values, DISTINCT), and
unknown scalar keys are dropped — a serializer that grows new fields in a
DuckDB upgrade cannot shift every definition hash. The running DuckDB
version is stamped into provenance.data regardless, so if a hash shift ever
does trace to an engine change, it is explainable.
"""

from __future__ import annotations

import hashlib
import json

from ..errors import SemanticsError
from .loader import Measure, Model

# scalar keys that carry meaning; everything else scalar is serializer noise
_KEEP = {"class", "type", "function_name", "schema", "distinct", "value",
         "is_operator", "catalog"}
_FOLD = {"function_name", "schema", "catalog"}      # SQL-case-insensitive


def _project(node):
    """The stable semantic subset of a json_serialize_sql tree."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if "location" in k:
                continue
            if k == "column_names":
                out[k] = [str(c).lower() for c in v]
            elif isinstance(v, (dict, list)):
                p = _project(v)
                if p not in ({}, []):
                    out[k] = p
            elif k in _KEEP:
                out[k] = v.lower() if k in _FOLD and isinstance(v, str) else v
        return out
    if isinstance(node, list):
        return [p for p in (_project(x) for x in node) if p not in ({}, [])]
    return node


def _fragment_projection(con, fact: str, sql: str, described: str):
    (raw,) = con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()
    tree = json.loads(raw)
    if tree.get("error"):
        raise SemanticsError(f"unparseable {described}: {tree.get('error_message')}")
    return _project(tree)


def measure_hash(con, model: Model, m: Measure) -> str:
    """Definition hash: expr + intrinsic where + ratio parts + time_agg +
    the fact reference. Prose (description) is deliberately NOT included."""
    parts = {}
    if m.expr is not None:
        parts["expr"] = _fragment_projection(
            con, model.fact, f"SELECT {m.expr} FROM {model.fact}",
            f"expr of '{m.name}'",
        )
    if m.where is not None:
        parts["where"] = _fragment_projection(
            con, model.fact, f"SELECT 1 FROM {model.fact} WHERE {m.where}",
            f"intrinsic predicate of '{m.name}'",
        )
    if m.ratio is not None:
        parts["num"] = _fragment_projection(
            con, model.fact, f"SELECT {m.ratio[0]} FROM {model.fact}",
            f"ratio num of '{m.name}'",
        )
        parts["den"] = _fragment_projection(
            con, model.fact, f"SELECT {m.ratio[1]} FROM {model.fact}",
            f"ratio den of '{m.name}'",
        )
    payload = {"parts": parts, "time_agg": m.time_agg, "fact": model.fact.lower()}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()


def model_hash(model: Model) -> str:
    """Everything about the model that determines results without being any
    single measure's definition: flipping `snapshot` changes what every
    measure MEANS while their measure hashes stay stable."""
    payload = {
        "fact": model.fact.lower(),
        "snapshot": model.snapshot,
        "time_column": model.time_column.lower(),
        "cadence": model.cadence,
        "fiscal_year_start": model.fiscal_year_start,
        "dims": {
            name: {
                "fact_column": ref.fact_column.lower(),
                "shared": None if ref.shared is None else {
                    "table": ref.shared.table.lower(),
                    "key_column": ref.shared.key_column.lower(),
                    "attributes": {
                        a: c.lower() for a, c in ref.shared.attributes.items()
                    },
                },
            }
            for name, ref in model.dims.items()
        },
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()
