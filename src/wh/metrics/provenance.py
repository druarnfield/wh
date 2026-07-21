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
from datetime import date, datetime

import duckdb

from ..errors import SemanticsError
from .loader import Measure, Model
from .timegrain import period_end

# scalar keys that carry meaning; everything else scalar is serializer noise
_KEEP = {"class", "type", "function_name", "schema", "distinct", "value",
         "is_operator", "catalog",
         "id", "try_cast"}   # type descriptors: CAST targets are semantics
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


def _iso(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat(sep=" ") if isinstance(v, datetime) else v.isoformat()
    return None if v is None else str(v)


def _measure_text(model: Model, m: Measure) -> str:
    if m.ratio is not None:
        text = f"{m.ratio[0]} / {m.ratio[1]}"
    else:
        text = m.expr
        if m.where:
            text = f"{text} FILTER (WHERE {m.where})"
    return f"{text} at snapshot" if model.snapshot else text


def capture_data(c, model: Model, compiled, args: dict) -> dict:
    """Everything data-side a number needs to explain itself, gathered on
    the connection (and at the moment) the number was computed. Slice.frame()
    memoises this so provenance() describes the executed result even if the
    mirror refreshes afterwards."""
    tables = {model.fact: None}
    for ref in model.dims.values():
        if ref.shared:
            tables[ref.shared.table] = None
    for t in tables:
        tables[t] = {
            "columns": {
                r[0]: r[1]
                for r in c.execute(f"DESCRIBE SELECT * FROM {t} LIMIT 0").fetchall()
            },
            "refreshed_at": _refreshed_at(c, t),
        }

    scan = {}
    if compiled.scan_lo:
        (fact_min,) = c.execute(
            f"SELECT min({model.time_column}) FROM {model.fact}"
        ).fetchone()
        for cmp, lo in compiled.scan_lo.items():
            if lo is None:
                coverage = "unbounded window"
            elif fact_min is not None and _as_dateval(fact_min) > _as_dateval(lo):
                coverage = (
                    f"fact data begins {_iso(fact_min)} — {cmp} window "
                    f"partially uncovered"
                )
            else:
                coverage = "fully covered"
            scan[cmp] = {"lo": _iso(lo), "coverage": coverage}

    as_at = None
    if compiled.asat_queries:
        as_at = {}
        for lane, sql in compiled.asat_queries.items():
            rows = sorted(c.execute(sql).fetchall())
            as_at[lane] = [
                [_iso(r[0]), _iso(r[1])] if len(r) == 2 else [None, _iso(r[0])]
                for r in rows
            ]

    truncation = None
    entries = args["ctx"].to_dict()
    time_entry = entries.get("time") if "time" in compiled.applied else None
    if time_entry and "between" in time_entry and args["grain"]:
        hi = date.fromisoformat(str(time_entry["between"][1])[:10])
        if hi < period_end(hi, args["grain"], model.fiscal_year_start):
            truncation = (
                f"final period truncated by context (as at {hi.isoformat()})"
            )

    return {
        "tables": tables,
        "duckdb_version": duckdb.__version__,
        "scan": scan,
        "as_at": as_at,
        "truncation": truncation,
    }


def _refreshed_at(c, table: str):
    parts = table.split(".")
    if len(parts) > 2:            # catalog-qualified: meta can't match it
        return None
    schema, name = parts if len(parts) == 2 else ("main", parts[0])
    try:
        (ts,) = c.execute(
            "SELECT max(extracted_at) FROM _mirror.meta "
            "WHERE schema_name = ? AND table_name = ?", [schema, name],
        ).fetchone()
    except duckdb.Error:
        return None                      # no _mirror.meta (not a mirror file)
    return _iso(ts) if ts is not None else None


class Provenance:
    """Everything a number needs to explain itself: what was computed,
    under what definition version, filtered how, on data from when."""

    def __init__(self, model: Model, compiled, args: dict, con, warnings=(),
                 data=None):
        c = con()
        self._model = model
        self._warnings_in = list(warnings)
        self.sql = compiled.sql
        self._model_hash = model_hash(model)
        self.measures = [
            (name, _measure_text(model, model.measures[name]),
             measure_hash(c, model, model.measures[name]))
            for name in args["measures"]
        ]

        ctx = args["ctx"]
        entries = ctx.to_dict()
        applied, unfiltered = {}, []
        for key in compiled.applied:
            if entries.get(key) == {"all": True}:
                unfiltered.append(key)
            elif key in entries:
                applied[key] = entries[key]
        self.context = {
            "applied": applied,
            "ignored": list(compiled.ignored),
            "unfiltered": unfiltered,
        }

        self.shape = {
            "by": list(args["by"]),
            "grain": args["grain"],
            "compare": list(args["compare"]),
            "complete_periods": args["complete_periods"],
            "suppress": args["suppress"],
            "non_additive": [
                n for n in args["measures"] if not model.measures[n].additive
            ],
            "strictness_weakened": bool(
                model.strict_context and args["strict_context"] is False
            ),
        }

        # warnings and model hash are definition-side — they belong to this
        # object, not the execution-time capture
        self.data = (
            dict(data) if data is not None
            else capture_data(c, model, compiled, args)
        )
        self.data["warnings"] = list(self._warnings_in)
        self.data["model_hash"] = self._model_hash

    # -- output --

    def render(self) -> str:
        """The sentence a number should be able to say for itself."""
        mh = self._model_hash[:6]
        lines = [
            f"{name} [{h[:6]} · model {mh}] = {text}"
            for name, text, h in self.measures
        ]
        ctx_bits = [
            f"{k} {'in ' + repr(list(v['in'])) if 'in' in v else ''}"
            f"{'= ' + str(v['eq']) if 'eq' in v else ''}"
            f"{'not ' + str(v['not']) if 'not' in v else ''}"
            f"{'in ' + str(v['between'][0]) + '..' + str(v['between'][1]) if 'between' in v else ''}"
            for k, v in self.context["applied"].items()
        ]
        ctx_bits += [f"{k}: empty selection → unfiltered"
                     for k in self.context["unfiltered"]]
        if ctx_bits:
            lines.append("context: " + "; ".join(ctx_bits))
        if self.context["ignored"]:
            lines.append("ignored (not on this model): "
                         + ", ".join(self.context["ignored"]))
        s = self.shape
        shape_bits = [b for b in [
            "by " + ", ".join(s["by"]) if s["by"] else "",
            f"grain {s['grain']}" if s["grain"] else "",
            "compare " + ", ".join(s["compare"]) if s["compare"] else "",
            "complete periods only" if s["complete_periods"] else "",
            f"suppressed under {s['suppress']}" if s["suppress"] else "",
            "strictness weakened" if s["strictness_weakened"] else "",
        ] if b]
        if shape_bits:
            lines.append(" · ".join(shape_bits))
        for cmp, sc in self.data["scan"].items():
            lines.append(f"{cmp}: scan widened to {sc['lo']}, {sc['coverage']}")
        if self.data["as_at"]:
            for lane, rows in self.data["as_at"].items():
                if rows:      # "FY25 as-at Jun 30 vs FY24 as-at Jun 28"
                    label = "snapshot" if lane == "base" else lane
                    lines.append(f"{label} as-at {rows[-1][1]} (latest period)")
        if self.data["truncation"]:
            lines.append(self.data["truncation"])
        if s["non_additive"]:
            lines.append(
                "non-additive: " + ", ".join(s["non_additive"])
                + " — do not re-sum result rows"
            )
        for w in self.data["warnings"]:
            lines.append(f"warning: {w}")
        refreshed = [
            f"{t} as-at {info['refreshed_at']}"
            for t, info in self.data["tables"].items() if info["refreshed_at"]
        ]
        lines.append(
            ("data " + "; ".join(refreshed))
            if refreshed
            else "refresh timestamps unavailable (no _mirror.meta)"
        )
        lines.append(f"duckdb {self.data['duckdb_version']}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "measures": [
                {"name": n, "text": t, "hash": h} for n, t, h in self.measures
            ],
            "context": self.context,
            "shape": self.shape,
            "data": self.data,
            "sql": self.sql,
        }


def _as_dateval(v):
    return v.date() if isinstance(v, datetime) else v
