"""Bind-time integrity checks: run once per (connection, model) with real
schemas in hand. Errors raise SemanticsError; data-quality findings come
back as warning strings (provenance later stamps them onto results).
"""

from __future__ import annotations

import json

import duckdb

from ..errors import SemanticsError
from .compiler import compile_slice
from .loader import Model


def bind_checks(con, model: Model) -> list[str]:
    """Raise on definition errors; return data-quality warnings."""
    _check_fact_only_refs(con, model)
    _check_measures_aggregate(con, model)
    _explain_representative_query(con, model)
    warnings: list[str] = []
    seen_tables = set()
    for dname, ref in model.dims.items():
        dim = ref.shared
        if dim is None:
            continue
        if dim.table not in seen_tables:
            seen_tables.add(dim.table)
            _check_dim_key_unique(con, dim)
        w = _orphan_warning(con, model, dname, ref)
        if w:
            warnings.append(w)
    return warnings


def _fact_columns(con, model: Model) -> set[str]:
    try:
        rows = con.execute(f"DESCRIBE SELECT * FROM {model.fact} LIMIT 0").fetchall()
    except duckdb.Error as e:
        raise SemanticsError(f"model '{model.name}': cannot read {model.fact}: {e}") from e
    return {r[0].lower() for r in rows}


def _column_refs(con, sql: str, described: str) -> list[list[str]]:
    (raw,) = con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()
    tree = json.loads(raw)
    if tree.get("error"):
        raise SemanticsError(
            f"unparseable {described}: {tree.get('error_message')}"
        )
    refs: list[list[str]] = []

    def walk(node):
        if isinstance(node, dict):
            if "column_names" in node:
                refs.append(node["column_names"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(tree)
    return refs


def _measure_parts(m) -> list[tuple[str, str, str]]:
    """(label, sql fragment, wrapping query) per definition piece."""
    parts = []
    if m.where:
        parts.append(("intrinsic predicate", m.where, "SELECT 1 FROM {fact} WHERE {x}"))
    if m.expr:
        parts.append(("expr", m.expr, "SELECT {x} FROM {fact}"))
    if m.ratio:
        parts.append(("ratio num", m.ratio[0], "SELECT {x} FROM {fact}"))
        parts.append(("ratio den", m.ratio[1], "SELECT {x} FROM {fact}"))
    return parts


def _check_fact_only_refs(con, model: Model) -> None:
    """Identity lives in git; dimension state does not — a definition
    depending on a type-1 attribute would mutate under a stable hash.
    Applies to exprs and ratio parts as much as to intrinsic predicates."""
    cols = _fact_columns(con, model)
    for m in model.measures.values():
        for label, fragment, template in _measure_parts(m):
            sql = template.format(fact=model.fact, x=fragment)
            for ref in _column_refs(con, sql, f"{label} of measure '{m.name}'"):
                if len(ref) > 1 or ref[0].lower() not in cols:
                    raise SemanticsError(
                        f"model '{model.name}': measure '{m.name}': {label} may "
                        f"reference fact columns only — '{'.'.join(ref)}' is "
                        f"not a column of {model.fact}"
                    )


def _check_measures_aggregate(con, model: Model) -> None:
    """An aggregate over zero rows yields exactly one row; a per-row expr
    yields zero. A non-aggregate expr would become a grouping column under
    GROUP BY ALL and silently change the result grain."""
    for m in model.measures.values():
        for label, fragment, template in _measure_parts(m):
            if label == "intrinsic predicate":
                continue
            try:
                rows = con.execute(
                    f"SELECT {fragment} FROM {model.fact} WHERE 1=0"
                ).fetchall()
            except duckdb.Error as e:
                raise SemanticsError(
                    f"model '{model.name}': measure '{m.name}': {label} does "
                    f"not compile: {e}"
                ) from e
            if len(rows) != 1:
                raise SemanticsError(
                    f"model '{model.name}': measure '{m.name}': {label} must "
                    f"be an aggregate expression — '{fragment}' returns one "
                    f"value per fact row, which would silently regroup results"
                )


def _explain_representative_query(con, model: Model) -> None:
    """EXPLAIN the widest slice: every measure, every declared attribute."""
    by = []
    for dname, ref in model.dims.items():
        if ref.shared:
            by += [f"{dname}.{a}" for a in ref.shared.attributes]
        else:
            by.append(dname)
    sql = compile_slice(model, list(model.measures), by=by, grain="month").sql
    try:
        con.execute(f"EXPLAIN {sql}")
    except duckdb.Error as e:
        raise SemanticsError(f"model '{model.name}' failed validation: {e}") from e


def _check_dim_key_unique(con, dim) -> None:
    n, d = con.execute(
        f"SELECT count(*), count(DISTINCT {dim.key_column}) FROM {dim.table}"
    ).fetchone()
    if n != d:
        raise SemanticsError(
            f"dimension table {dim.table} has duplicate {dim.key_column} values "
            f"({n} rows, {d} distinct) — join fan-out would silently inflate "
            f"every non-distinct measure; deduplicate the dimension"
        )


def _orphan_warning(con, model: Model, dname: str, ref) -> str | None:
    dim = ref.shared
    orphans, rows, total = con.execute(f"""
        SELECT count(DISTINCT f.{ref.fact_column}) FILTER (WHERE d.{dim.key_column} IS NULL),
               count(*) FILTER (WHERE d.{dim.key_column} IS NULL),
               count(*)
        FROM {model.fact} AS f
        LEFT JOIN {dim.table} AS d ON f.{ref.fact_column} = d.{dim.key_column}
        WHERE f.{ref.fact_column} IS NOT NULL
    """).fetchone()
    if not orphans:
        return None
    pct = 100.0 * rows / total if total else 0.0
    return (
        f"{dname}: {orphans} orphan key(s) in {model.fact}.{ref.fact_column} "
        f"({pct:.1f}% of rows) — they survive unfiltered totals but group "
        f"into a NULL row under by= and vanish under attribute filters"
    )
