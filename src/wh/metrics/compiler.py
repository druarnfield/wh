"""Single-fact slice compilation: the two lanes ARE the emitted shape.

Intrinsic measure predicates compile as `agg FILTER (WHERE ...)`; the
extrinsic context compiles into the outer WHERE. On snapshot models the
as-at subquery carries time-context predicates ONLY — never attribute
predicates (the evaluation moment must not move under extrinsic input).
Dialect is DuckDB, deliberately: GROUP BY ALL, FILTER.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..errors import SemanticsError
from .context_ops import All, Between, Context, Eq, In, Not, EMPTY
from .loader import Model
from .timegrain import grain_expr


@dataclass(frozen=True)
class Compiled:
    sql: str
    applied: tuple   # context keys compiled into the outer WHERE
    ignored: tuple   # context keys skipped by the miss rule


def _lit(v) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, date):
        return f"DATE '{v.isoformat()}'"
    return "'" + str(v).replace("'", "''") + "'"


def _predicate(lhs: str, op) -> str | None:
    if isinstance(op, All):
        return None
    if isinstance(op, Eq):
        return f"{lhs} = {_lit(op.value)}"
    if isinstance(op, In):
        return f"{lhs} IN ({', '.join(_lit(x) for x in op.values)})"
    if isinstance(op, Not):
        return f"{lhs} <> {_lit(op.value)}"
    if isinstance(op, Between):
        return f"{lhs} BETWEEN {_lit(op.lo)} AND {_lit(op.hi)}"
    raise SemanticsError(f"cannot compile context op {type(op).__name__}")


def _declared_surface(model: Model) -> str:
    parts = ["time"]
    for name, ref in model.dims.items():
        if ref.shared:
            parts += [f"{name}__{a}" for a in ref.shared.attributes]
        else:
            parts.append(name)
    return ", ".join(parts)


def split_context(model: Model, ctx: Context):
    """The miss rule: entries apply by shared-dim identity (or local-dim
    name); anything else is skipped and recorded. Returns
    (time_op | None, [(key, lhs_sql, op)], applied_keys, ignored_keys)."""
    if not ctx.is_resolved:
        raise SemanticsError(
            "context holds widgets or relative time — resolve() it first "
            "(slice() does this for you)"
        )
    time_op, attrs, applied, ignored = None, [], [], []
    for key, op in ctx.entries.items():
        if key == "time":
            time_op = op
            applied.append(key)
            continue
        dname, _, attr = key.partition("__")
        ref = model.dims.get(dname)
        if ref is None:
            ignored.append(key)
            continue
        if ref.shared is None:
            if attr:
                raise SemanticsError(
                    f"local dimension '{dname}' has no attributes — use plain "
                    f"'{dname}='"
                )
            lhs = f"fact.{ref.fact_column}"
        else:
            if not attr:
                raise SemanticsError(
                    f"context entry '{dname}' needs an attribute, e.g. "
                    f"{dname}__{next(iter(ref.shared.attributes))}"
                )
            col = ref.shared.attributes.get(attr)
            if col is None:
                raise SemanticsError(
                    f"dimension '{dname}' has no attribute '{attr}' "
                    f"(declared: {', '.join(ref.shared.attributes)})"
                )
            lhs = f"{dname}.{col}"
        attrs.append((key, lhs, op))
        applied.append(key)
    return time_op, attrs, tuple(applied), tuple(ignored)


def _by_item(model: Model, entry: str) -> tuple[str, str | None]:
    """-> (select item, joined dim name or None)."""
    dname, _, attr = entry.partition(".")
    ref = model.dims.get(dname)
    if ref is None:
        raise SemanticsError(
            f"unknown by= entry '{entry}' — declared surface: "
            f"{_declared_surface(model)}"
        )
    if ref.shared is None:
        if attr:
            raise SemanticsError(
                f"local dimension '{dname}' has no attributes — use plain "
                f"'{dname}' in by="
            )
        return f"fact.{ref.fact_column} AS {dname}", None
    if not attr:
        raise SemanticsError(
            f"by= entry '{dname}' needs an attribute, e.g. "
            f"{dname}.{next(iter(ref.shared.attributes))}"
        )
    col = ref.shared.attributes.get(attr)
    if col is None:
        raise SemanticsError(
            f"dimension '{dname}' has no attribute '{attr}' "
            f"(declared: {', '.join(ref.shared.attributes)})"
        )
    return f'{dname}.{col} AS "{dname}.{attr}"', dname


def _measure_sql(m) -> str:
    body = m.expr
    if m.where:
        body = f"{body} FILTER (WHERE {m.where})"
    return body


def compile_slice(
    model: Model,
    measures: list[str],
    by: list[str] = (),
    ctx: Context = EMPTY,
    grain: str | None = None,
) -> Compiled:
    for name in measures:
        if name not in model.measures:
            raise SemanticsError(
                f"model '{model.name}' has no measure '{name}' "
                f"(available: {', '.join(model.measures)})"
            )

    time_op, ctx_attrs, applied, ignored = split_context(model, ctx)

    select: list[str] = []
    outer: list[str] = []        # projection of the ratio wrapper, if needed
    joined: set[str] = set()
    if grain is not None:
        expr = grain_expr(grain, f"fact.{model.time_column}", model.fiscal_year_start)
        select.append(f"{expr} AS period")
        outer.append("period")
    for entry in by:
        item, dim = _by_item(model, entry)
        select.append(item)
        outer.append(item.rsplit(" AS ", 1)[1])
        if dim:
            joined.add(dim)
    has_ratio = False
    for name in measures:
        m = model.measures[name]
        if m.ratio:
            has_ratio = True
            num, den = m.ratio
            select.append(f"{num} AS __{name}_num")
            select.append(f"{den} AS __{name}_den")
            outer.append(
                f"CAST(__{name}_num AS DOUBLE) / NULLIF(__{name}_den, 0) AS {name}"
            )
        else:
            select.append(f"{_measure_sql(m)} AS {name}")
            outer.append(name)

    predicates: list[str] = []
    if time_op is not None:
        p = _predicate(f"fact.{model.time_column}", time_op)
        if p:
            predicates.append(p)
    for _key, lhs, op in ctx_attrs:
        p = _predicate(lhs, op)
        if p:
            predicates.append(p)
        if "." in lhs.removeprefix("fact."):
            joined.add(lhs.split(".", 1)[0])

    lines = ["SELECT " + ",\n       ".join(select), f"FROM {model.fact} AS fact"]
    for dname in [d for d in model.dims if d in joined]:   # declaration order
        dim = model.dims[dname].shared
        lines.append(
            f"LEFT JOIN {dim.table} AS {dname} "
            f"ON fact.{model.dims[dname].fact_column} = {dname}.{dim.key_column}"
        )
    if predicates:
        lines.append("WHERE " + "\n  AND ".join(predicates))
    lines.append("GROUP BY ALL")
    if has_ratio:
        # division outermost, __num/__den carried beneath — never re-aggregated
        inner = "\n".join("    " + line for line in "\n".join(lines).splitlines())
        lines = ["SELECT " + ",\n       ".join(outer), "FROM (", inner, ")"]
    if grain is not None:
        lines.append("ORDER BY period")
    return Compiled(sql="\n".join(lines), applied=applied, ignored=ignored)
