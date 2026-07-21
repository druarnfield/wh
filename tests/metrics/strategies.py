"""Generative vocabulary: semantic specs both sides can interpret.

The spec is the shared language between the compiler under test and the
oracle. This module MAY import wh (it builds real Models); the oracle
may NOT."""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from hypothesis import strategies as st

from wh.metrics.loader import DimRef, Measure, Model, SharedDim

CATS = ["Cat 1", "Cat 2", "Cat 3"]
REGIONS = ["North", "South"]
DIM_KEYS = ["K1", "K2", "K3"]          # in the dim table
ORPHAN = "K9"                           # never in the dim table
ENTS = ["U1", "U2", "U3", "U4"]
DATA_LO, DATA_HI = date(2025, 6, 1), date(2026, 9, 30)

# measure spec -> (SQL fragmentry, oracle meaning). Closed vocabulary.
MEASURES = {
    "n":    ("count",),
    "amt":  ("sum", "v"),
    "ents": ("nunique", "k"),
    "big":  ("count_where_gt", "v", 50),
    "pct":  ("ratio_gt", "v", 50),
}
SNAPSHOT_MEASURES = ["n", "ents", "big", "pct"]   # sum is a load error on stocks


@dataclass(frozen=True)
class Case:
    rows: tuple                  # fact rows: dicts d/k/c/v/fk
    dim_rows: tuple              # (key, region) pairs — dim table content
    snapshot: bool
    fys: int                     # fiscal_year_start
    cadence: str | None = None
    measures: dict = field(default_factory=lambda: dict(MEASURES))


@dataclass(frozen=True)
class SliceArgs:
    measures: tuple
    by: tuple                    # subset of ("c", "facility.region")
    ctx: dict                    # oracle-op form: key -> ("eq",v)|("in",vs)|("not",v)|("all",)
    time: tuple | None           # (lo, hi) dates or datetimes, lo <= hi
    grain: str | None
    compare: tuple = ()
    complete_periods: bool = False
    suppress: int | None = None


def _dates(draw):
    span = (DATA_HI - DATA_LO).days
    return DATA_LO + timedelta(days=draw(st.integers(0, span)))


@st.composite
def _rows(draw, snapshot):
    n = draw(st.integers(5, 60))
    use_ts = draw(st.booleans())
    if snapshot:
        # a handful of census dates, several rows per census
        censuses = sorted({_dates(draw) for _ in range(draw(st.integers(2, 6)))})
        days = draw(st.lists(st.sampled_from(censuses), min_size=n, max_size=n))
    else:
        days = [_dates(draw) for _ in range(n)]
    rows = []
    for d in days:
        t = datetime.combine(d, time(draw(st.integers(0, 23)), 30)) if use_ts else d
        rows.append({
            "d": t,
            "k": draw(st.sampled_from(ENTS)),
            "c": draw(st.sampled_from(CATS + [None])),
            "v": draw(st.one_of(st.none(), st.integers(0, 100))),
            "fk": draw(st.sampled_from(DIM_KEYS + [ORPHAN, None])),
        })
    return tuple(rows)


@st.composite
def cases(draw):
    snapshot = draw(st.booleans())
    ms = dict(MEASURES)
    if snapshot:
        ms = {k: v for k, v in ms.items() if k in SNAPSHOT_MEASURES}
    return Case(
        rows=draw(_rows(snapshot)),
        dim_rows=tuple((k, draw(st.sampled_from(REGIONS))) for k in DIM_KEYS),
        snapshot=snapshot,
        fys=draw(st.sampled_from([1, 7])),
        cadence="weekly" if snapshot and draw(st.booleans()) else None,
        measures=ms,
    )


def _ctx_op(draw, pool):
    kind = draw(st.sampled_from(["eq", "in", "not", "all"]))
    if kind == "eq":
        return ("eq", draw(st.sampled_from(pool)))
    if kind == "in":
        return ("in", tuple(draw(st.lists(st.sampled_from(pool), min_size=1,
                                          max_size=2, unique=True))))
    if kind == "not":
        return ("not", draw(st.sampled_from(pool)))
    return ("all",)


@st.composite
def scenarios(draw, with_compare=False, with_suppress=False,
              with_complete=False):
    case = draw(cases())
    measures = tuple(draw(st.lists(st.sampled_from(sorted(case.measures)),
                                   min_size=1, max_size=3, unique=True)))
    by = tuple(draw(st.lists(st.sampled_from(["c", "facility.region"]),
                             max_size=2, unique=True)))
    ctx = {}
    if draw(st.booleans()):
        ctx["c"] = _ctx_op(draw, CATS)
    if draw(st.booleans()):
        ctx["facility.region"] = _ctx_op(draw, REGIONS)
    win = None
    if draw(st.booleans()):
        a, b = sorted([_dates(draw), _dates(draw)])
        if draw(st.booleans()):
            win = (datetime.combine(a, time(3, 0)),
                   datetime.combine(b, time(21, 0)))
        else:
            win = (a, b)
    grain = draw(st.sampled_from(
        [None, "day", "week", "month", "quarter", "year", "fy", "fy_quarter"]
    ))
    compare = ()
    if with_compare and grain is not None:
        opts = ["prior"] + (["yoy"] if grain != "week" else [])
        if not case.snapshot:
            opts.append("fytd")
        compare = tuple(draw(st.lists(st.sampled_from(opts), min_size=1,
                                      max_size=2, unique=True)))
    suppress = draw(st.sampled_from([None, 1, 2, 3])) if with_suppress else None
    complete = (with_complete and grain is not None and draw(st.booleans()))
    return case, SliceArgs(measures=measures, by=by, ctx=ctx, time=win,
                           grain=grain, compare=compare,
                           complete_periods=complete, suppress=suppress)


# --- compiler-side construction ---

def build_model(case: Case) -> Model:
    shared = SharedDim(name="facility", table="dim_fac", key_column="fk",
                       attributes={"region": "region"})
    default_agg = "last" if case.snapshot else "sum"
    measures = {}
    for name, spec in case.measures.items():
        kind = spec[0]
        if kind == "count":
            measures[name] = Measure(name, "x", expr="count(*)",
                                     time_agg=default_agg)
        elif kind == "sum":
            measures[name] = Measure(name, "x", expr=f"sum({spec[1]})",
                                     time_agg=default_agg)
        elif kind == "nunique":
            measures[name] = Measure(name, "x",
                                     expr=f"count(DISTINCT {spec[1]})",
                                     time_agg=default_agg, additive=False)
        elif kind == "count_where_gt":
            measures[name] = Measure(name, "x", expr="count(*)",
                                     where=f"{spec[1]} > {spec[2]}",
                                     time_agg=default_agg)
        elif kind == "ratio_gt":
            measures[name] = Measure(
                name, "x", time_agg=default_agg, additive=False,
                ratio=(f"count(*) FILTER (WHERE {spec[1]} > {spec[2]})",
                       "count(*)"),
            )
    return Model(
        name="gen", fact="f", description="", time_column="d",
        cadence=case.cadence, snapshot=case.snapshot,
        dims={"c": DimRef(shared=None, fact_column="c"),
              "facility": DimRef(shared=shared, fact_column="fk")},
        measures=measures, strict_context=False, fiscal_year_start=case.fys,
    )


def render_yaml(case: Case) -> str:
    """The same spec as YAML text — the fidelity test proves loader parity."""
    default_agg = "last" if case.snapshot else "sum"
    lines = [
        "dimensions:",
        "  facility:",
        "    table: dim_fac",
        "    key_column: fk",
        "    attributes: {region: region}",
        "gen:",
        "  fact: f",
        "  time:",
        "    column: d",
    ]
    if case.cadence:
        lines.append(f"    cadence: {case.cadence}")
    if case.snapshot:
        lines.append("  snapshot: true")
    lines += ["  dimensions:", "    c: c", "    facility: {shared: fk}",
              "  measures:"]
    for name, spec in case.measures.items():
        kind = spec[0]
        if kind == "ratio_gt":
            lines += [f"    {name}:", "      description: x", "      ratio:",
                      f"        num: \"count(*) FILTER (WHERE {spec[1]} > {spec[2]})\"",
                      "        den: \"count(*)\"",
                      f"      time_agg: {default_agg}"]
            continue
        expr = {"count": lambda: "count(*)", "sum": lambda: f"sum({spec[1]})",
                "nunique": lambda: f"count(DISTINCT {spec[1]})",
                "count_where_gt": lambda: "count(*)"}[kind]()
        lines += [f"    {name}:", "      description: x",
                  f"      expr: \"{expr}\"", f"      time_agg: {default_agg}"]
        if kind == "count_where_gt":
            lines.append(f"      where: \"{spec[1]} > {spec[2]}\"")
    return "\n".join(lines) + "\n"


def seed(con, case: Case) -> None:
    ts = any(isinstance(r["d"], datetime) for r in case.rows)
    con.execute(f"CREATE TABLE f (d {'TIMESTAMP' if ts else 'DATE'}, "
                "k VARCHAR, c VARCHAR, v INTEGER, fk VARCHAR)")
    con.executemany("INSERT INTO f VALUES (?,?,?,?,?)",
                    [(r["d"], r["k"], r["c"], r["v"], r["fk"])
                     for r in case.rows])
    con.execute("CREATE TABLE dim_fac (fk VARCHAR, region VARCHAR)")
    con.executemany("INSERT INTO dim_fac VALUES (?,?)", list(case.dim_rows))


def to_wh_context(args: SliceArgs):
    """Translate oracle-op form into a real wh context."""
    from wh.metrics.context_ops import All, Between, Context, Eq, In, Not

    def op(t):
        return {"eq": lambda: Eq(t[1]), "in": lambda: In(tuple(t[1])),
                "not": lambda: Not(t[1]), "all": lambda: All()}[t[0]]()

    entries = {}
    if "c" in args.ctx:
        entries["c"] = op(args.ctx["c"])
    if "facility.region" in args.ctx:
        entries["facility__region"] = op(args.ctx["facility.region"])
    if args.time is not None:
        entries["time"] = Between(args.time[0], args.time[1])
    return Context(entries)
