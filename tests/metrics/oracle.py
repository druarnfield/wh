"""The boring oracle: a deliberately naive interpreter of the documented
metrics semantics over plain Python rows.

MUST NEVER import wh.* — independence from the compiler's date arithmetic
and NULL handling is exactly what makes agreement evidence. Duplication
with src/wh/metrics is deliberate; do not 'refactor' it away."""

from datetime import date, datetime, time, timedelta
from math import isclose


def _day(t):
    return t.date() if isinstance(t, datetime) else t


def period_of(t, grain, fys):
    d = _day(t)
    if grain == "day":
        return d
    if grain == "week":
        return d - timedelta(days=d.weekday())
    if grain == "month":
        return date(d.year, d.month, 1)
    if grain == "quarter":
        return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)
    if grain == "year":
        return date(d.year, 1, 1)
    fy_year = d.year if d.month >= fys else d.year - 1
    if grain == "fy":
        return date(fy_year, fys, 1)
    if grain == "fy_quarter":
        months_in = (d.year - fy_year) * 12 + d.month - fys        # 0..11
        m0 = fy_year * 12 + (fys - 1) + 3 * (months_in // 3)
        return date(m0 // 12, m0 % 12 + 1, 1)
    raise ValueError(grain)


def in_window(t, win):
    """Documented contract: date bounds are day-inclusive both ends;
    datetime bounds are exact instants."""
    if win is None:
        return True
    lo, hi = win
    tl = t if isinstance(t, datetime) else datetime.combine(t, time.min)
    lod = lo if isinstance(lo, datetime) else datetime.combine(lo, time.min)
    if isinstance(hi, datetime):
        return lod <= tl <= hi
    return lod <= tl < datetime.combine(hi, time.min) + timedelta(days=1)


def passes(val, op):
    kind = op[0]
    if kind == "all":
        return True
    if kind == "eq":
        return val is not None and val == op[1]
    if kind == "in":
        return val is not None and val in op[1]
    if kind == "not":                     # IS DISTINCT FROM: NULLs are kept
        return val is None or val != op[1]
    raise ValueError(op)


def _attr(case, row, name):
    if name == "c":
        return row["c"]
    if name == "facility.region":
        dim = dict(case.dim_rows)
        return dim.get(row["fk"])         # orphan or NULL fk -> None
    raise ValueError(name)


def _asat_filter(case, rows, grain):
    """Per period (or globally), keep only rows at the max time value.
    Runs on time-windowed rows ONLY — attribute context must not move it."""
    def bucket(r):
        return period_of(r["d"], grain, case.fys) if grain else 0
    mx = {}
    for r in rows:
        b = bucket(r)
        if b not in mx or r["d"] > mx[b]:
            mx[b] = r["d"]
    return [r for r in rows if r["d"] == mx[bucket(r)]]


def _agg(spec, rs):
    kind = spec[0]
    if kind == "count":
        return len(rs)
    if kind == "sum":
        vals = [r[spec[1]] for r in rs if r[spec[1]] is not None]
        return sum(vals) if vals else None
    if kind == "nunique":
        return len({r[spec[1]] for r in rs if r[spec[1]] is not None})
    if kind == "count_where_gt":
        return len([r for r in rs
                    if r[spec[1]] is not None and r[spec[1]] > spec[2]])
    if kind == "ratio_gt":
        den = len(rs)
        num = len([r for r in rs
                   if r[spec[1]] is not None and r[spec[1]] > spec[2]])
        return None if den == 0 else num / den
    raise ValueError(spec)


def slice_oracle(case, measures, by=(), ctx=None, win=None, grain=None):
    """-> {(period?, *by_values): {measure: value}} — only nonempty groups,
    matching GROUP BY ALL. With no grouping columns at all the slice is an
    ungrouped SQL aggregate: exactly one row even over an empty selection."""
    ctx = dict(ctx or {})
    rows = [r for r in case.rows if in_window(r["d"], win)]
    if case.snapshot:
        rows = _asat_filter(case, rows, grain)
    for key, op in ctx.items():
        rows = [r for r in rows if passes(_attr(case, r, key), op)]
    groups = {}
    if grain is None and not by:
        groups[()] = []
    for r in rows:
        gk = ((period_of(r["d"], grain, case.fys),) if grain else ()) + \
             tuple(_attr(case, r, b) for b in by)
        groups.setdefault(gk, []).append(r)
    return {
        gk: {m: _agg(case.measures[m], rs) for m in measures}
        for gk, rs in groups.items()
    }


# --- comparator ---

def result_map(res, by, measures, grain, compare=()):
    """DuckDB cursor/relation result -> the oracle's map shape. The
    column-order assert doubles as the output-namespace guarantee: the
    compiler interleaves each measure with its comparison columns."""
    cols = [d[0] for d in res.description]
    rows = res.fetchall()
    value_cols = [c for m in measures
                  for c in [m] + [f"{m}_{cmp}" for cmp in compare]]
    want = (["period"] if grain else []) + list(by) + value_cols
    assert cols == want, f"column mismatch: {cols} != {want}"
    out = {}
    for row in rows:
        d = dict(zip(cols, row))
        gk = ((d["period"],) if grain else ()) + tuple(d[b] for b in by)
        assert gk not in out, f"duplicate group {gk}"
        out[gk] = {m: d[m] for m in value_cols}
    return out


def assert_maps_equal(actual, expected, context=""):
    assert set(actual) == set(expected), (
        f"{context} group keys differ:\n only-compiled: "
        f"{sorted(set(actual) - set(expected), key=repr)}\n only-oracle:   "
        f"{sorted(set(expected) - set(actual), key=repr)}"
    )
    for gk in expected:
        for m, ev in expected[gk].items():
            av = actual[gk][m]
            if ev is None or av is None:
                assert av is None and ev is None, \
                    f"{context} {gk}/{m}: compiled={av!r} oracle={ev!r}"
            elif isinstance(ev, float) or isinstance(av, float):
                assert isclose(float(av), float(ev), rel_tol=1e-9, abs_tol=1e-12), \
                    f"{context} {gk}/{m}: compiled={av!r} oracle={ev!r}"
            else:
                assert av == ev, f"{context} {gk}/{m}: compiled={av!r} oracle={ev!r}"
