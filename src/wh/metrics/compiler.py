"""Single-fact slice compilation: the two lanes ARE the emitted shape.

Intrinsic measure predicates compile as `agg FILTER (WHERE ...)`; the
extrinsic context compiles into the outer WHERE. On snapshot models the
as-at subquery carries time-context predicates ONLY — never attribute
predicates (the evaluation moment must not move under extrinsic input).
Dialect is DuckDB, deliberately: GROUP BY ALL, FILTER.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..errors import SemanticsError
from .context_ops import All, Between, Context, Eq, In, Not, EMPTY, _months_back
from .loader import Model
from .timegrain import PERIOD_INTERVAL, fy_start, grain_expr

_CADENCE_DAYS = {"daily": 1, "weekly": 7, "monthly": 31}


@dataclass(frozen=True)
class Compiled:
    sql: str
    applied: tuple   # context keys compiled into the outer WHERE
    ignored: tuple   # context keys skipped by the miss rule
    scan_lo: dict | None = None   # compare -> widened window start (provenance)
    asat_queries: dict | None = None   # lane -> as-at subquery (snapshot models)


COMPARES = ("prior", "yoy", "fytd")

_PRIOR_OFFSET = {   # (months, days) one grain-period back
    "day": (0, 1), "week": (0, 7), "month": (1, 0), "quarter": (3, 0),
    "year": (12, 0), "fy": (12, 0), "fy_quarter": (3, 0),
}


def _compare_offset(cmp: str, grain: str) -> tuple[int, int]:
    return (12, 0) if cmp == "yoy" else _PRIOR_OFFSET[grain]


def _shift_back(v, months: int, days: int):
    if months:
        d = _months_back(v, months)
        v = datetime.combine(d, v.time()) if isinstance(v, datetime) else d
    return v - timedelta(days=days) if days else v


def _shift_op(op, months: int, days: int):
    if not isinstance(op, Between):
        return op
    return Between(_shift_back(op.lo, months, days), _shift_back(op.hi, months, days))


def _check_compare(model: Model, measures, compare, grain) -> None:
    if not compare:
        return
    if grain is None:
        raise SemanticsError("compare= needs grain= (a period axis to shift along)")
    for cmp in compare:
        if cmp not in COMPARES:
            raise SemanticsError(
                f"unknown compare '{cmp}' — valid: {', '.join(COMPARES)}"
            )
        if cmp == "yoy" and grain == "week":
            raise SemanticsError(
                "yoy at week grain misaligns week starts — use month or coarser"
            )
    for name in measures:
        m = model.measures[name]
        if m.time_agg == "none":
            raise SemanticsError(
                f"measure '{name}' has time_agg none — no comparisons are "
                f"defined for it"
            )
        if m.time_agg in ("last", "avg") and "fytd" in compare:
            raise SemanticsError(
                f"fytd is cumulative; measure '{name}' is a point-in-time stock "
                f"(time_agg {m.time_agg}) — model the flow as its own "
                f"event-grain fact"
            )
        for cmp in compare:
            if f"{name}_{cmp}" in model.measures:
                raise SemanticsError(
                    f"comparison column '{name}_{cmp}' collides with a declared "
                    f"measure — rename one of them"
                )


def _lit(v) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, datetime):        # BEFORE date — datetime is a date subclass
        return f"TIMESTAMP '{v.isoformat(sep=' ')}'"
    if isinstance(v, date):
        return f"DATE '{v.isoformat()}'"
    if not isinstance(v, str):
        raise SemanticsError(
            f"context values must be scalars or dates — got {type(v).__name__} ({v!r})"
        )
    return "'" + v.replace("'", "''") + "'"


def _predicate(lhs: str, op) -> str | None:
    if isinstance(op, All):
        return None
    if isinstance(op, Eq):
        return f"{lhs} = {_lit(op.value)}"
    if isinstance(op, In):
        return f"{lhs} IN ({', '.join(_lit(x) for x in op.values)})"
    if isinstance(op, Not):
        # IS DISTINCT FROM: "not Cat 3" plainly includes rows where the
        # attribute is NULL/unknown — <> would silently drop them
        return f"{lhs} IS DISTINCT FROM {_lit(op.value)}"
    if isinstance(op, Between):
        return f"{lhs} BETWEEN {_lit(op.lo)} AND {_lit(op.hi)}"
    raise SemanticsError(f"cannot compile context op {type(op).__name__}")


def _time_predicate(lhs: str, op) -> str | None:
    """Time ranges are day-inclusive on both ends. Plain-date bounds compile
    as `>= lo AND < hi + 1 day` so a TIMESTAMP time column keeps the whole
    end day (BETWEEN ... DATE 'hi' would cut at midnight and, on snapshot
    models, silently move the as-at moment). Datetime bounds are exact."""
    if not isinstance(op, Between):
        return _predicate(lhs, op)
    hi = (
        f"{lhs} <= {_lit(op.hi)}"
        if isinstance(op.hi, datetime)
        else f"{lhs} < {_lit(op.hi)} + INTERVAL 1 DAY"
    )
    return f"{lhs} >= {_lit(op.lo)} AND {hi}"


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
                ignored.append(key)      # can't apply here — miss rule, not error
                continue
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


def _by_surface(model: Model) -> str:
    parts = []
    for name, ref in model.dims.items():
        if ref.shared:
            parts += [f"{name}.{a}" for a in ref.shared.attributes]
        else:
            parts.append(name)
    return ", ".join(parts) or "none (no dimensions declared)"


def _by_item(model: Model, entry: str) -> tuple[str, str | None]:
    """-> (select item, joined dim name or None)."""
    dname, _, attr = entry.partition(".")
    if dname == "time":
        raise SemanticsError("time is sliced via grain=, not by=")
    ref = model.dims.get(dname)
    if ref is None:
        raise SemanticsError(
            f"unknown by= entry '{entry}' — declared surface: {_by_surface(model)}"
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


def _asat_subquery(model: Model, grain: str | None, time_op) -> str:
    """The as-at moment(s): global max snapshot per period. Time-context
    predicates only — an attribute filter must never move the moment a
    measure is evaluated at. Also run standalone by provenance."""
    tc = model.time_column
    time_pred = _time_predicate(tc, time_op) if time_op is not None else None
    where = f" WHERE {time_pred}" if time_pred else ""
    if grain is None:
        return f"SELECT max({tc}) AS __as_at FROM {model.fact}{where}"
    g_bare = grain_expr(grain, tc, model.fiscal_year_start)
    return (
        f"SELECT {g_bare} AS __period, max({tc}) AS __as_at\n"
        f"      FROM {model.fact}{where} GROUP BY 1"
    )


def _asat_join(model: Model, grain: str | None, time_op) -> list[str]:
    sub = _asat_subquery(model, grain, time_op)
    tc = model.time_column
    if grain is None:
        return [f"JOIN ({sub}) AS __asat", f"  ON fact.{tc} = __asat.__as_at"]
    g_fact = grain_expr(grain, f"fact.{tc}", model.fiscal_year_start)
    return [
        f"JOIN ({sub}) AS __asat",
        f"  ON {g_fact} = __asat.__period AND fact.{tc} = __asat.__as_at",
    ]


def _inner_lines(model, measures, by, ctx_attrs, time_op, grain, cell_n=False):
    """One aggregate query (SELECT ... GROUP BY ALL, no ORDER BY) — reused
    per comparison CTE with a shifted time_op. cell_n adds the hidden
    per-cell row count that suppression reads.
    -> (lines, group_aliases, has_ratio)."""
    select: list[str] = []
    joined: set[str] = set()
    group_aliases: list[str] = []
    if grain is not None:
        expr = grain_expr(grain, f"fact.{model.time_column}", model.fiscal_year_start)
        select.append(f"{expr} AS period")
        group_aliases.append("period")
    for entry in by:
        item, dim = _by_item(model, entry)
        select.append(item)
        group_aliases.append(item.rsplit(" AS ", 1)[1])
        if dim:
            joined.add(dim)
    has_ratio = False
    for name in measures:
        m = model.measures[name]
        if m.ratio:
            has_ratio = True
            select.append(f"{m.ratio[0]} AS __{name}_num")
            select.append(f"{m.ratio[1]} AS __{name}_den")
        else:
            select.append(f"{_measure_sql(m)} AS {name}")
    if cell_n:
        select.append("count(*) AS __cell_n")

    predicates: list[str] = []
    if time_op is not None:
        p = _time_predicate(f"fact.{model.time_column}", time_op)
        if p:
            predicates.append(p)
    for _key, lhs, op in ctx_attrs:
        p = _predicate(lhs, op)
        if p:
            predicates.append(p)
        if "." in lhs.removeprefix("fact."):
            joined.add(lhs.split(".", 1)[0])

    lines = ["SELECT " + ",\n       ".join(select), f"FROM {model.fact} AS fact"]
    if model.snapshot:
        lines += _asat_join(model, grain, time_op)
    for dname in [d for d in model.dims if d in joined]:   # declaration order
        dim = model.dims[dname].shared
        lines.append(
            f"LEFT JOIN {dim.table} AS {dname} "
            f"ON fact.{model.dims[dname].fact_column} = {dname}.{dim.key_column}"
        )
    if predicates:
        lines.append("WHERE " + "\n  AND ".join(predicates))
    lines.append("GROUP BY ALL")
    return lines, group_aliases, has_ratio


def _sel(prefix: str, name: str, m, alias: str | None = None,
         suppress: int | None = None) -> str:
    """Outer-select item for a measure column living in `prefix` (a CTE
    alias, or '' inside the plain wrapper). With suppress, values from
    cells under n rows go NULL — and a ratio also nulls when its own
    denominator is under n (a big cell can hide a tiny denominator)."""
    p = f"{prefix}." if prefix else ""
    if m.ratio:
        e = f"CAST({p}__{name}_num AS DOUBLE) / NULLIF({p}__{name}_den, 0)"
        if suppress is not None:
            e = (
                f"CASE WHEN {p}__cell_n < {suppress} "
                f"OR {p}__{name}_den < {suppress} THEN NULL ELSE {e} END"
            )
        return f"{e} AS {alias or name}"
    if suppress is not None:
        return (
            f"CASE WHEN {p}__cell_n < {suppress} THEN NULL ELSE {p}{name} END "
            f"AS {alias or name}"
        )
    return f"{p}{name}" + (f" AS {alias}" if alias else "")


def _indent(lines: list[str]) -> str:
    return "\n".join("    " + line for line in "\n".join(lines).splitlines())


def _complete_predicate(model: Model, grain: str, period_ref: str) -> str:
    """Keep only complete periods. Snapshot models with a declared cadence:
    the period's final EXPECTED snapshot landed. Otherwise: the max-date
    rule (period end within the data), which is weaker on snapshot models.
    Data max comes from the fact — _mirror.meta records refresh time, not
    data max."""
    tc = model.time_column
    period_end = f"{period_ref} + INTERVAL {PERIOD_INTERVAL[grain]} - INTERVAL 1 DAY"
    if model.snapshot and model.cadence in _CADENCE_DAYS:
        days = _CADENCE_DAYS[model.cadence]
        g = grain_expr(grain, tc, model.fiscal_year_start)
        return (
            f"(SELECT max({tc}) FROM {model.fact} WHERE {g} = {period_ref})"
            f" > {period_end} - INTERVAL {days} DAY"
        )
    return f"{period_end} <= (SELECT max({tc}) FROM {model.fact})"


def compile_slice(
    model: Model,
    measures: list[str],
    by: list[str] = (),
    ctx: Context = EMPTY,
    grain: str | None = None,
    compare: list[str] = (),
    complete_periods: bool = False,
    suppress: int | None = None,
) -> Compiled:
    if not measures:
        raise SemanticsError("slice() needs at least one measure")
    if complete_periods and grain is None:
        raise SemanticsError("complete_periods needs grain= (periods to complete)")
    if suppress is not None and (not isinstance(suppress, int) or suppress < 1):
        raise SemanticsError("suppress(n) needs a positive integer threshold")
    for name in measures:
        if name not in model.measures:
            raise SemanticsError(
                f"model '{model.name}' has no measure '{name}' "
                f"(available: {', '.join(model.measures)})"
            )
    _check_compare(model, measures, compare, grain)

    time_op, ctx_attrs, applied, ignored = split_context(model, ctx)
    lines, group_aliases, has_ratio = _inner_lines(
        model, measures, by, ctx_attrs, time_op, grain, cell_n=suppress is not None
    )

    if compare:
        return _assemble_compare(
            model, measures, by, ctx_attrs, time_op, grain, compare,
            lines, group_aliases, applied, ignored, complete_periods, suppress,
        )

    if has_ratio or complete_periods or suppress is not None:
        # division/completeness/suppression live in an outer level;
        # __num/__den/__cell_n carried beneath, never re-aggregated or leaked
        outer = list(group_aliases) + [
            _sel("", name, model.measures[name], suppress=suppress)
            for name in measures
        ]
        lines = ["SELECT " + ",\n       ".join(outer), "FROM (", _indent(lines), ")"]
        if complete_periods:
            lines.append("WHERE " + _complete_predicate(model, grain, "period"))
    if grain is not None:
        lines.append("ORDER BY period")
    asat = {"base": _asat_subquery(model, grain, time_op)} if model.snapshot else None
    return Compiled(
        sql="\n".join(lines), applied=applied, ignored=ignored, asat_queries=asat
    )


def _fytd_lines(model, measures, by, ctx_attrs, time_op, grain, group_aliases,
                cell_n=False):
    """Fiscal year-to-date, recomputed from base rows per output period —
    never a window-sum of period aggregates, so distinct counts and every
    other aggregate stay correct-from-base. Fact rows join back to their
    OWN group's period rows (IS NOT DISTINCT FROM per by-column) — a
    time-only join would silently absorb every other group's rows.
    Snapshot models never reach here (fytd is invalid for stocks)."""
    tc = f"fact.{model.time_column}"
    fys = model.fiscal_year_start
    sel = [f"p.{a}" for a in group_aliases]
    for name in measures:
        m = model.measures[name]
        if m.ratio:
            sel.append(f"{m.ratio[0]} AS __{name}_num")
            sel.append(f"{m.ratio[1]} AS __{name}_den")
        else:
            sel.append(f"{_measure_sql(m)} AS {name}")
    if cell_n:
        sel.append("count(*) AS __cell_n")

    predicates: list[str] = []
    joined: set[str] = set()
    if isinstance(time_op, Between):     # mid-period truncation carries in
        hi = (
            f"{tc} <= {_lit(time_op.hi)}"
            if isinstance(time_op.hi, datetime)
            else f"{tc} < {_lit(time_op.hi)} + INTERVAL 1 DAY"
        )
        predicates.append(hi)
    for _key, lhs, op in ctx_attrs:
        p = _predicate(lhs, op)
        if p:
            predicates.append(p)
        if "." in lhs.removeprefix("fact."):
            joined.add(lhs.split(".", 1)[0])

    for entry in by:              # each fact row counts toward ITS group only
        item, dim = _by_item(model, entry)
        expr, alias = item.rsplit(" AS ", 1)
        predicates.append(f"{expr} IS NOT DISTINCT FROM p.{alias}")
        if dim:
            joined.add(dim)       # dim joins land before the WHERE, so this is legal

    lines = [
        "SELECT " + ",\n       ".join(sel),
        f"FROM (SELECT DISTINCT {', '.join(group_aliases)} FROM __out) AS p",
        f"JOIN {model.fact} AS fact",
        f"  ON {tc} >= {grain_expr('fy', 'p.period', fys)}",
        f" AND {grain_expr(grain, tc, fys)} <= p.period",
    ]
    for dname in [d for d in model.dims if d in joined]:
        dim = model.dims[dname].shared
        lines.append(
            f"LEFT JOIN {dim.table} AS {dname} "
            f"ON fact.{model.dims[dname].fact_column} = {dname}.{dim.key_column}"
        )
    if predicates:
        lines.append("WHERE " + "\n  AND ".join(predicates))
    lines.append("GROUP BY ALL")
    return lines


def _assemble_compare(
    model, measures, by, ctx_attrs, time_op, grain, compare,
    out_lines, group_aliases, applied, ignored, complete_periods=False,
    suppress=None,
) -> Compiled:
    """Comparisons are shifted CTEs self-joined back — never lag, so gap
    periods stay NULL instead of slipping. Each CTE carries its own window
    (and, on snapshot models, its own as-at)."""
    ctes: list[tuple[str, list[str]]] = [("__out", out_lines)]
    joins: list[str] = []
    scan_lo: dict = {}
    asat: dict | None = (
        {"base": _asat_subquery(model, grain, time_op)} if model.snapshot else None
    )
    group_conds = [
        f"IS NOT DISTINCT FROM b.{g}" for g in group_aliases if g != "period"
    ]
    for cmp in compare:
        name = f"__cmp_{cmp}"
        if cmp == "fytd":
            cte_lines = _fytd_lines(
                model, measures, by, ctx_attrs, time_op, grain, group_aliases,
                cell_n=suppress is not None,
            )
            scan_lo[cmp] = (
                fy_start(time_op.lo, model.fiscal_year_start)
                if isinstance(time_op, Between) else None
            )
            conds = [f"{name}.period = b.period"]
            ctes.append((name, cte_lines))
        else:
            months, days = _compare_offset(cmp, grain)
            shifted = _shift_op(time_op, months, days)
            cte_lines, _, _ = _inner_lines(
                model, measures, by, ctx_attrs, shifted, grain,
                cell_n=suppress is not None,
            )
            scan_lo[cmp] = shifted.lo if isinstance(shifted, Between) else None
            if asat is not None:
                asat[cmp] = _asat_subquery(model, grain, shifted)
            shift = f"INTERVAL {months} MONTH" if months else f"INTERVAL {days} DAY"
            conds = [f"{name}.period = b.period - {shift}"]
            ctes.append((name, cte_lines))
        conds += [
            f"{name}.{g} {cond}"
            for g, cond in zip(
                [g for g in group_aliases if g != "period"], group_conds
            )
        ]
        joins.append(f"LEFT JOIN {name}\n       ON " + "\n      AND ".join(conds))

    proj = [f"b.{g}" for g in group_aliases]
    for mname in measures:
        m = model.measures[mname]
        proj.append(_sel("b", mname, m, suppress=suppress))
        for cmp in compare:
            proj.append(
                _sel(f"__cmp_{cmp}", mname, m, alias=f"{mname}_{cmp}",
                     suppress=suppress)
            )

    with_block = ", ".join(f"{n} AS (\n{_indent(ls)}\n)" for n, ls in ctes)
    tail = []
    if complete_periods:
        tail.append("WHERE " + _complete_predicate(model, grain, "b.period"))
    sql = "\n".join([
        "WITH " + with_block,
        "SELECT " + ",\n       ".join(proj),
        "FROM __out AS b",
        *joins,
        *tail,
        "ORDER BY period",
    ])
    return Compiled(
        sql=sql, applied=applied, ignored=ignored, scan_lo=scan_lo,
        asat_queries=asat,
    )


def compile_values(model: Model, attr: str, ctx: Context = EMPTY) -> str:
    """Possible values for a declared attribute. Unscoped shared-dim options
    come from the DIMENSION TABLE — cheap, no fact scan. With a context,
    options re-derive from context-scoped fact rows through the normal
    lanes (no snapshot as-at: options mean "ever in scope"). NULL is never
    an option — membership tests exclude it anyway."""
    item, dim = _by_item(model, attr)
    lhs = item.rsplit(" AS ", 1)[0]
    if not ctx.entries and dim is not None:
        shared = model.dims[dim].shared
        col = shared.attributes[attr.partition(".")[2]]
        return (
            f"SELECT DISTINCT {col} AS value\nFROM {shared.table}\n"
            f"WHERE {col} IS NOT NULL\nORDER BY 1"
        )
    time_op, ctx_attrs, _applied, _ignored = split_context(model, ctx)
    joined = {dim} if dim else set()
    predicates: list[str] = []
    if time_op is not None:
        p = _time_predicate(f"fact.{model.time_column}", time_op)
        if p:
            predicates.append(p)
    for _key, p_lhs, op in ctx_attrs:
        p = _predicate(p_lhs, op)
        if p:
            predicates.append(p)
        if "." in p_lhs.removeprefix("fact."):
            joined.add(p_lhs.split(".", 1)[0])
    lines = [f"SELECT DISTINCT {lhs} AS value", f"FROM {model.fact} AS fact"]
    for dname in [d for d in model.dims if d in joined]:
        d = model.dims[dname].shared
        lines.append(
            f"LEFT JOIN {d.table} AS {dname} "
            f"ON fact.{model.dims[dname].fact_column} = {dname}.{d.key_column}"
        )
    lines.append("WHERE " + "\n  AND ".join(predicates + [f"{lhs} IS NOT NULL"]))
    lines.append("ORDER BY 1")
    return "\n".join(lines)
