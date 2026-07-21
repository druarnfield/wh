"""Metrics definitions: semantics/*.yml -> frozen dataclasses, load rules.

Pure — no DuckDB here. Rules that need real table schemas (intrinsic-where
column checks, dim-key uniqueness, EXPLAIN validation) run at bind time in
checks.py. Every error is one sentence plus the fix.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..errors import SemanticsError

VALID_TIME_AGG = ("sum", "last", "none")

# Names and columns are spliced into SQL unquoted — validate them at load.
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")
_RESERVED = frozenset({"fact", "period", "time"})   # compiler-owned aliases


def _check_name(fname: str, kind: str, name, reserved=_RESERVED) -> None:
    if (
        not isinstance(name, str)
        or not _IDENT.fullmatch(name)
        or name.startswith("__")
        or name.lower() in reserved
    ):
        raise SemanticsError(
            f"{fname}: invalid {kind} name {name!r} — letters, digits and "
            f"underscores only, not starting with '__', not fact/period/time"
        )


def _check_column(fname: str, kind: str, name) -> None:
    _check_name(fname, kind, name, reserved=frozenset())


def _check_table(fname: str, kind: str, name) -> None:
    if not isinstance(name, str) or not _TABLE.fullmatch(name):
        raise SemanticsError(
            f"{fname}: invalid {kind} {name!r} — plain schema.table identifiers only"
        )


def _reject_unknown(fname: str, where: str, mapping: dict, known: tuple) -> None:
    unknown = [str(k) for k in mapping if k not in known]
    if not unknown:
        return
    hint = difflib.get_close_matches(unknown[0], known, n=1)
    did = f" — did you mean '{hint[0]}'?" if hint else ""
    raise SemanticsError(
        f"{fname}: {where}: unknown key(s) {', '.join(map(repr, unknown))} "
        f"(valid: {', '.join(known)}){did}"
    )

# distinct counts, medians, modes: summing their result rows is meaningless
_IMPLICITLY_NON_ADDITIVE = re.compile(r"\bDISTINCT\b|\bmedian\s*\(|\bmode\s*\(", re.I)
_HAS_FILTER = re.compile(r"\bFILTER\b", re.I)


@dataclass(frozen=True)
class SharedDim:
    name: str
    table: str
    key_column: str
    attributes: dict           # attr name -> dim-table column
    hierarchy: tuple = ()      # attr names, finest first


@dataclass(frozen=True)
class Measure:
    name: str
    description: str
    expr: str | None = None            # DuckDB aggregate SQL over fact columns
    where: str | None = None           # intrinsic predicate — part of identity
    ratio: tuple | None = None         # (num_expr, den_expr)
    time_agg: str = "sum"              # sum | last | avg | none
    additive: bool = True              # effective flag (implicit rules applied)


@dataclass(frozen=True)
class DimRef:
    shared: SharedDim | None           # None = local (degenerate) dim
    fact_column: str                   # fact-side key, or the fact column itself


@dataclass(frozen=True)
class Model:
    name: str
    fact: str
    description: str
    time_column: str
    cadence: str | None
    snapshot: bool
    dims: dict                         # name -> DimRef
    measures: dict                     # name -> Measure
    strict_context: bool
    fiscal_year_start: int


def load_definitions(directory: Path, fiscal_year_start: int) -> dict:
    """Merge semantics/*.yml|*.yaml into {model_name: Model}.

    Top-level key `dimensions:` declares shared dims (defined once, ever);
    every other top-level key is a model."""
    dims: dict[str, SharedDim] = {}
    dim_origin: dict[str, str] = {}
    raw_models: dict[str, tuple[str, dict]] = {}
    for f in sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")]):
        try:
            raw = yaml.safe_load(f.read_text())
        except yaml.YAMLError as e:
            raise SemanticsError(f"{f.name}: invalid YAML: {e}") from e
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise SemanticsError(f"{f.name}: root must be a mapping")
        for key, spec in raw.items():
            if key == "dimensions":
                _parse_shared_dims(f.name, spec, dims, dim_origin)
            elif key in raw_models:
                raise SemanticsError(
                    f"model '{key}' defined in both {raw_models[key][0]} and {f.name}"
                )
            else:
                raw_models[key] = (f.name, spec)
    return {
        name: _parse_model(fname, name, spec, dims, fiscal_year_start)
        for name, (fname, spec) in raw_models.items()
    }


def _parse_shared_dims(fname, spec, dims, dim_origin) -> None:
    if not isinstance(spec, dict):
        raise SemanticsError(f"{fname}: 'dimensions:' must be a mapping")
    for name, d in spec.items():
        if name in dim_origin:
            raise SemanticsError(
                f"shared dimension '{name}' defined in both {dim_origin[name]} "
                f"and {fname} — shared dims are defined exactly once"
            )
        if isinstance(d, dict):
            _reject_unknown(
                fname, f"dimension '{name}'", d,
                ("table", "key_column", "attributes", "hierarchy"),
            )
        if not isinstance(d, dict) or not d.get("table") or not d.get("key_column"):
            raise SemanticsError(
                f"{fname}: dimension '{name}' needs 'table' and 'key_column' keys"
            )
        _check_name(fname, "dimension", name)
        _check_table(fname, "dimension table", d["table"])
        _check_column(fname, "key_column", d["key_column"])
        attrs = d.get("attributes")
        if not isinstance(attrs, dict) or not attrs or not all(
            isinstance(v, str) for v in attrs.values()
        ):
            raise SemanticsError(
                f"{fname}: dimension '{name}' needs 'attributes:' mapping "
                f"attribute names to columns"
            )
        for attr, col in attrs.items():
            _check_column(fname, f"attribute of '{name}'", attr)
            _check_column(fname, f"attribute column of '{name}'", col)
        hierarchy = tuple(d.get("hierarchy") or ())
        for level in hierarchy:
            if level not in attrs:
                raise SemanticsError(
                    f"{fname}: dimension '{name}': hierarchy entry '{level}' is "
                    f"not a declared attribute (have: {', '.join(attrs)})"
                )
        dims[name] = SharedDim(
            name=name, table=str(d["table"]), key_column=str(d["key_column"]),
            attributes=dict(attrs), hierarchy=hierarchy,
        )
        dim_origin[name] = fname


def _parse_model(fname, name, spec, shared_dims, fiscal_year_start) -> Model:
    if not isinstance(spec, dict) or not isinstance(spec.get("fact"), str):
        raise SemanticsError(f"{fname}: model '{name}' needs a 'fact:' key (a table)")
    _reject_unknown(
        fname, f"model '{name}'", spec,
        ("fact", "description", "time", "snapshot", "dimensions",
         "measures", "strict_context"),
    )
    _check_table(fname, "fact table", spec["fact"])
    time = spec.get("time")
    if not isinstance(time, dict) or not isinstance(time.get("column"), str):
        raise SemanticsError(
            f"{fname}: model '{name}' needs 'time:' with a 'column:' key"
        )
    _reject_unknown(fname, f"model '{name}': time:", time, ("column", "cadence"))
    _check_column(fname, "time column", time["column"])
    cadence = time.get("cadence")
    if cadence is not None and cadence not in ("daily", "weekly", "monthly"):
        raise SemanticsError(
            f"{fname}: model '{name}': cadence must be daily, weekly or "
            f"monthly (got {cadence!r})"
        )
    snapshot = bool(spec.get("snapshot", False))

    dims: dict[str, DimRef] = {}
    for dname, v in (spec.get("dimensions") or {}).items():
        _check_name(fname, "dimension", dname)
        if isinstance(v, dict):
            _reject_unknown(
                fname, f"model '{name}': dimension '{dname}'", v, ("shared",)
            )
            col = v.get("shared")
            if not isinstance(col, str):
                raise SemanticsError(
                    f"{fname}: model '{name}': dimension '{dname}': 'shared:' "
                    f"takes the fact-side key column, e.g. "
                    f"{dname}: {{shared: {dname}_code}}"
                )
            if dname not in shared_dims:
                raise SemanticsError(
                    f"{fname}: model '{name}': dimension '{dname}' references "
                    f"a shared dimension that doesn't exist — declare it under "
                    f"a top-level 'dimensions:' block (or drop 'shared:' for a "
                    f"local dim)"
                )
            _check_column(fname, f"fact column of '{dname}'", col)
            dims[dname] = DimRef(shared=shared_dims[dname], fact_column=col)
            continue
        if not isinstance(v, str):
            raise SemanticsError(
                f"{fname}: model '{name}': dimension '{dname}' must be a fact "
                f"column (local dim) or {{shared: <fact key column>}}"
            )
        if dname in shared_dims:
            raise SemanticsError(
                f"{fname}: model '{name}': dimension '{dname}' is also a "
                f"shared dimension — write {dname}: {{shared: {v}}} to "
                f"reference it, or rename the local dim"
            )
        _check_column(fname, f"fact column of '{dname}'", v)
        dims[dname] = DimRef(shared=None, fact_column=v)

    raw_measures = spec.get("measures")
    if not isinstance(raw_measures, dict) or not raw_measures:
        raise SemanticsError(f"{fname}: model '{name}' declares no measures")
    measures = {
        mname: _parse_measure(fname, name, mname, m, snapshot)
        for mname, m in raw_measures.items()
    }

    return Model(
        name=name, fact=spec["fact"], description=str(spec.get("description", "")),
        time_column=time["column"],
        cadence=str(time["cadence"]) if time.get("cadence") is not None else None,
        snapshot=snapshot, dims=dims, measures=measures,
        strict_context=bool(spec.get("strict_context", False)),
        fiscal_year_start=fiscal_year_start,
    )


def _parse_measure(fname, model, mname, m, snapshot) -> Measure:
    where = f"model '{model}': measure '{mname}'"
    _check_name(fname, "measure", mname)
    if not isinstance(m, dict):
        raise SemanticsError(f"{fname}: {where} must be a mapping")
    _reject_unknown(
        fname, where, m,
        ("description", "expr", "where", "ratio", "time_agg", "additive"),
    )
    if not m.get("description"):
        raise SemanticsError(
            f"{fname}: {where} needs a description — an auditable definition "
            f"without prose isn't one"
        )
    expr, ratio = m.get("expr"), m.get("ratio")
    if (expr is None) == (ratio is None):
        raise SemanticsError(
            f"{fname}: {where} needs exactly one of 'expr:' or 'ratio:'"
        )
    if ratio is not None:
        if isinstance(ratio, dict):
            _reject_unknown(fname, f"{where}: ratio:", ratio, ("num", "den"))
        if not isinstance(ratio, dict) or not ratio.get("num") or not ratio.get("den"):
            raise SemanticsError(
                f"{fname}: {where}: 'ratio:' needs both 'num:' and 'den:'"
            )
        if m.get("where"):
            raise SemanticsError(
                f"{fname}: {where}: 'where:' on a ratio is ambiguous — put the "
                f"predicate in the num/den FILTER clauses"
            )
        ratio = (str(ratio["num"]), str(ratio["den"]))
    intrinsic = str(m["where"]) if m.get("where") is not None else None
    if expr is not None and intrinsic is not None and _HAS_FILTER.search(str(expr)):
        raise SemanticsError(
            f"{fname}: {where}: expr already has a FILTER clause — fold the "
            f"'where:' predicate into it"
        )

    time_agg = m.get("time_agg", "last" if snapshot else "sum")
    if time_agg == "avg":
        raise SemanticsError(
            f"{fname}: {where}: time_agg 'avg' isn't computed anywhere yet — "
            f"snapshot models always read the period's final snapshot; use "
            f"'last' until averaging-over-snapshots is implemented"
        )
    if time_agg not in VALID_TIME_AGG:
        raise SemanticsError(
            f"{fname}: {where}: time_agg must be one of {', '.join(VALID_TIME_AGG)}"
        )
    if snapshot and time_agg == "sum":
        raise SemanticsError(
            f"{fname}: {where}: time_agg 'sum' on a snapshot model undercounts "
            f"(mid-period rows drop off before the final snapshot) — model the "
            f"flow as its own event-grain fact"
        )

    texts = " ".join(filter(None, [str(expr or "")] + list(ratio or ())))
    additive = (
        m.get("additive", True) is not False
        and ratio is None
        and not _IMPLICITLY_NON_ADDITIVE.search(texts)
    )
    return Measure(
        name=mname, description=str(m["description"]),
        expr=str(expr) if expr is not None else None,
        where=intrinsic, ratio=ratio, time_agg=time_agg, additive=additive,
    )
