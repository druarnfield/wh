# Metrics Trust-Contract Test Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Layer generative, differential, metamorphic, mutation, and fuzz
testing over the existing structural suite so the semantic compiler's
correctness claims are backed by an independent oracle and a measured
harness — complete, proactive, defensible.

**Architecture:** A closed *semantic-spec* vocabulary (small dataclasses)
is the shared language: Hypothesis generates specs + datasets, one side
renders them into real `Model`s for the compiler, the other side is a
deliberately naive pure-Python interpreter (the oracle) that never imports
`wh.*`. Cell-exact agreement between the two implementations over
randomized cases is the core evidence. Metamorphic properties cover
spec-level consistency the oracle can't (prior==direct, partition sums),
mutation testing measures whether the whole suite bites, and totality
fuzzing proves the loader firewall never fails open or crashes.

**Tech Stack:** Python 3.13, pytest, Hypothesis (new dev dep), mutmut (new
dev dep), DuckDB in-memory. No CI exists — heavy budgets are opt-in via
`HYPOTHESIS_PROFILE=deep` and on-demand scripts, documented in a new
`docs/testing.md`.

**Standing invariants this plan creates (add to CLAUDE.md in Task 10):**
- `tests/metrics/oracle.py` must NEVER import `wh.*` — independence from
  the compiler's date arithmetic and NULL handling is what makes agreement
  evidence. Sharing "just a helper" silently correlates the bugs away.
- Generative tests carry `@pytest.mark.fuzz`; the default profile keeps
  `uv run pytest` fast; `HYPOTHESIS_PROFILE=deep` is the nightly-strength
  run.

**Conventions:** helpers live in uniquely-named modules (`oracle.py`,
`strategies.py`) — never imported from conftest (two conftests on
sys.path). TDD adapts for test infrastructure: each harness layer is
validated by proving it *detects a planted wrong answer* before trusting
its green. Full-suite gate before every commit:
`set -o pipefail; uv run pytest -q`.

---

## Task 1: Dependencies, profiles, markers, doc skeleton

**Files:**
- Modify: `pyproject.toml`
- Modify: `tests/metrics/conftest.py`
- Modify: `.gitignore`
- Create: `docs/testing.md`

- [x] **Step 1: Add dev dependencies and pytest marker config**

Run: `uv add --dev hypothesis mutmut`

In `pyproject.toml` add (new section at the end):

```toml
[tool.pytest.ini_options]
markers = [
    "fuzz: generative (Hypothesis) tests — budget controlled by HYPOTHESIS_PROFILE",
]
```

- [x] **Step 2: Register Hypothesis profiles**

Append to `tests/metrics/conftest.py`:

```python
import os

from hypothesis import HealthCheck, settings

settings.register_profile(
    "default", max_examples=40, deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "deep", max_examples=2000, deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))
```

Add `.hypothesis/` to `.gitignore`.

- [x] **Step 3: Create `docs/testing.md` skeleton**

```markdown
# Testing the metrics layer — the trust contract

The metrics layer computes governed numbers; its test harness is layered
so that correctness is *demonstrated*, not just asserted:

1. **Structural tests** (`tests/metrics/test_*.py`) — parse-tree SQL
   assertions, canary-literal lane isolation, and a regression museum of
   every past adversarial finding.
2. **Differential tests** (`test_differential.py`) — randomized slices
   checked cell-exact against an independent naive interpreter
   (`oracle.py`, which never imports `wh.*`).
3. **Metamorphic properties** (`test_properties.py`) — relations between
   the compiler's own answers (partition sums, window splitting,
   prior==direct) that hold regardless of implementation.
4. **Totality fuzzing** (`test_fuzz_loader.py`) — every input either
   loads or raises `SemanticsError`; generated SQL always parses.
5. **Mutation testing** (on demand) — measures whether the suite would
   catch seeded defects. Baseline scores below.
6. **Engine matrix** (`scripts/engine_matrix.sh`) — the suite against the
   pinned and latest DuckDB.

## Running

- Fast (default, every commit): `uv run pytest`
- Deep generative run: `HYPOTHESIS_PROFILE=deep uv run pytest tests/metrics -m fuzz`
- Mutation: see the Mutation section below.
- Engine matrix: `bash scripts/engine_matrix.sh`

## Guarantee traceability

(filled in as tasks land; final table in Task 10)

## Mutation baseline

(recorded in Task 9)
```

- [x] **Step 4: Verify and commit**

Run: `set -o pipefail; uv run pytest -q` — count unchanged, no warnings
about unknown markers.

```bash
git add pyproject.toml uv.lock tests/metrics/conftest.py .gitignore docs/testing.md
git commit -m "Add hypothesis/mutmut, fuzz marker, testing doc skeleton"
```

---

## Task 2: The semantic-spec vocabulary and generators

The shared language: a `Case` (dataset + model semantics) and `SliceArgs`,
generated together by one composite so they're always consistent. One side
builds real `Model` dataclasses; a fidelity property proves the YAML
rendering of a spec loads to the identical model, so the loader path stays
covered without paying file IO per example.

**Files:**
- Create: `tests/metrics/strategies.py`
- Test: `tests/metrics/test_strategies.py`

- [x] **Step 1: Write `tests/metrics/strategies.py`**

```python
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
        expr = {"count": "count(*)", "sum": f"sum({spec[1]})",
                "nunique": f"count(DISTINCT {spec[1]})",
                "count_where_gt": "count(*)"}[kind]
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
```

- [x] **Step 2: Write the generator self-tests**

Create `tests/metrics/test_strategies.py`:

```python
"""The generators must produce loadable, bindable, faithful specs —
otherwise every downstream layer tests garbage."""

import duckdb
import pytest
from hypothesis import given, settings

from wh.metrics.checks import bind_checks
from wh.metrics.loader import load_definitions

from strategies import build_model, cases, render_yaml, scenarios, seed


@pytest.mark.fuzz
@given(case=cases())
def test_rendered_yaml_loads_to_the_same_model(case, tmp_path_factory):
    d = tmp_path_factory.mktemp("sem")
    (d / "gen.yml").write_text(render_yaml(case))
    loaded = load_definitions(d, fiscal_year_start=case.fys)["gen"]
    assert loaded == build_model(case)


@pytest.mark.fuzz
@settings(max_examples=15)
@given(sc=scenarios())
def test_generated_cases_pass_bind_checks(sc):
    case, _args = sc
    con = duckdb.connect()
    seed(con, case)
    bind_checks(con, build_model(case))     # warnings fine, no raise
```

Note: `Model`/`Measure` are frozen dataclasses so `==` is fieldwise; if
the fidelity assert fails on a benign default (e.g. `description`), make
`render_yaml`/`build_model` agree rather than weakening the assert.

- [x] **Step 3: Run, fix generator bugs until green**

Run: `uv run pytest tests/metrics/test_strategies.py -q`
Expected: PASS. Shrunken counterexamples here are generator bugs — fix in
`strategies.py`, never by loosening the test.

- [x] **Step 4: Full suite gate and commit**

```bash
set -o pipefail; uv run pytest -q
git add tests/metrics/strategies.py tests/metrics/test_strategies.py
git commit -m "Generative spec vocabulary for the metrics layer

One composite generates dataset + model + slice-args consistently; a
fidelity property proves the YAML rendering loads to the identical Model,
keeping the loader in the differential loop without per-example file IO."
```

---

## Task 3: The oracle and the comparator

**Files:**
- Create: `tests/metrics/oracle.py`
- Test: `tests/metrics/test_oracle.py`

- [x] **Step 1: Write `tests/metrics/oracle.py` (plain slices)**

```python
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
    matching GROUP BY ALL."""
    ctx = dict(ctx or {})
    rows = [r for r in case.rows if in_window(r["d"], win)]
    if case.snapshot:
        rows = _asat_filter(case, rows, grain)
    for key, op in ctx.items():
        rows = [r for r in rows if passes(_attr(case, r, key), op)]
    groups = {}
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
```

- [x] **Step 2: Comparator + oracle self-tests**

Create `tests/metrics/test_oracle.py`:

```python
"""The comparator must detect wrong answers, or every green is vacuous.
Plus pinned oracle behaviors on tiny hand-checked datasets."""

import pytest
from datetime import date

from oracle import assert_maps_equal, in_window, passes, period_of


def test_comparator_detects_wrong_value():
    with pytest.raises(AssertionError, match="compiled=2"):
        assert_maps_equal({(): {"n": 2}}, {(): {"n": 3}})


def test_comparator_detects_missing_and_extra_groups():
    with pytest.raises(AssertionError, match="group keys differ"):
        assert_maps_equal({("a",): {"n": 1}}, {("b",): {"n": 1}})


def test_comparator_detects_null_vs_zero():
    with pytest.raises(AssertionError):
        assert_maps_equal({(): {"amt": 0}}, {(): {"amt": None}})


def test_oracle_period_arithmetic_pinned():
    assert period_of(date(2026, 6, 30), "fy", 7) == date(2025, 7, 1)
    assert period_of(date(2026, 7, 1), "fy", 7) == date(2026, 7, 1)
    assert period_of(date(2026, 8, 15), "fy_quarter", 7) == date(2026, 7, 1)
    assert period_of(date(2026, 6, 15), "fy_quarter", 7) == date(2026, 4, 1)
    assert period_of(date(2026, 1, 5), "week", 1) == date(2025, 12, 29)


def test_oracle_not_keeps_nulls():
    assert passes(None, ("not", "Cat 1"))
    assert not passes(None, ("eq", "Cat 1"))


def test_oracle_windows_are_day_inclusive():
    assert in_window(date(2026, 6, 30), (date(2026, 6, 1), date(2026, 6, 30)))
    assert not in_window(date(2026, 7, 1), (date(2026, 6, 1), date(2026, 6, 30)))
```

- [x] **Step 3: Run, then commit**

Run: `uv run pytest tests/metrics/test_oracle.py -v` — all pass.

```bash
set -o pipefail; uv run pytest -q
git add tests/metrics/oracle.py tests/metrics/test_oracle.py
git commit -m "Naive oracle interpreter and self-testing comparator

The oracle reimplements the documented semantics over plain rows with no
wh imports — independence is the evidence. Comparator self-tests prove a
planted wrong value, wrong group set, or NULL-vs-zero is detected."
```

---

## Task 4: Differential testing — plain slices

**Files:**
- Create: `tests/metrics/test_differential.py`

- [x] **Step 1: Write the differential test + the harness-bites meta-test**

```python
"""Differential: compiled SQL vs the naive oracle, cell-exact, over
randomized cases. The meta-test proves the harness detects a planted
off-by-one before any green is trusted."""

import duckdb
import pytest
from hypothesis import given, note

from wh.metrics.compiler import compile_slice

from oracle import assert_maps_equal, result_map, slice_oracle
from strategies import build_model, scenarios, seed, to_wh_context


def run_both(case, args):
    con = duckdb.connect()
    seed(con, case)
    model = build_model(case)
    compiled = compile_slice(
        model, list(args.measures), by=list(args.by),
        ctx=to_wh_context(args), grain=args.grain,
        compare=list(args.compare),
        complete_periods=args.complete_periods, suppress=args.suppress,
    )
    note(compiled.sql)
    actual = result_map(
        con.execute(compiled.sql), args.by, args.measures, args.grain,
        compare=args.compare,
    )
    return actual, compiled


@pytest.mark.fuzz
@given(sc=scenarios())
def test_plain_slices_match_the_oracle(sc):
    case, args = sc
    actual, _ = run_both(case, args)
    expected = slice_oracle(case, args.measures, by=args.by, ctx=args.ctx,
                            win=args.time, grain=args.grain)
    assert_maps_equal(actual, expected)


def test_the_harness_bites():
    """Plant the classic bound bug (< widened to <=) in emitted SQL and
    prove the comparator catches it — otherwise green is vacuous."""
    from datetime import date
    from strategies import Case, MEASURES, SliceArgs

    case = Case(
        rows=(
            {"d": date(2026, 6, 30), "k": "U1", "c": "Cat 1", "v": 1, "fk": "K1"},
            {"d": date(2026, 7, 1), "k": "U2", "c": "Cat 1", "v": 1, "fk": "K1"},
        ),
        dim_rows=(("K1", "North"), ("K2", "South"), ("K3", "North")),
        snapshot=False, fys=7, measures=dict(MEASURES),
    )
    args = SliceArgs(measures=("n",), by=(), ctx={},
                     time=(date(2026, 6, 1), date(2026, 6, 30)), grain=None)
    con = duckdb.connect()
    seed(con, case)
    compiled = compile_slice(build_model(case), ["n"], ctx=to_wh_context(args))
    corrupted = compiled.sql.replace("< DATE", "<= DATE")
    assert corrupted != compiled.sql
    actual = result_map(con.execute(corrupted), (), ("n",), None)
    expected = slice_oracle(case, ("n",), win=args.time)
    with pytest.raises(AssertionError):
        assert_maps_equal(actual, expected)
```

- [x] **Step 2: Run at default budget, then one deep shakeout**

Run: `uv run pytest tests/metrics/test_differential.py -q`
Then: `HYPOTHESIS_PROFILE=deep uv run pytest tests/metrics/test_differential.py -q`

Failures at this stage are as likely oracle bugs as compiler bugs — the
shrunken example decides. Diagnose against the documented contract in
`docs/metrics.md`; fix whichever side disagrees with the *doc*. If the doc
itself is ambiguous, that's a finding: clarify the doc in the same commit.

- [x] **Step 3: Commit**

```bash
set -o pipefail; uv run pytest -q
git add tests/metrics/test_differential.py
git commit -m "Differential testing: compiled slices vs the oracle

Randomized datasets and slice shapes (grain/by/context/snapshot/time,
date and datetime bounds) checked cell-exact against the independent
interpreter. A meta-test plants the classic <-vs-<= bound bug in emitted
SQL and proves the harness catches it."
```

---

## Task 5: Differential coverage for compare= (prior / yoy / fytd)

**Files:**
- Modify: `tests/metrics/oracle.py`
- Modify: `tests/metrics/test_differential.py`

- [x] **Step 1: Extend the oracle**

Append to `oracle.py`:

```python
import calendar


def _months_back_naive(d, n):
    y, m = divmod(d.year * 12 + d.month - 1 - n, 12)
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


_OFFSETS = {  # grain -> (months, days) for 'prior'; yoy is always (12, 0)
    "day": (0, 1), "week": (0, 7), "month": (1, 0), "quarter": (3, 0),
    "year": (12, 0), "fy": (12, 0), "fy_quarter": (3, 0),
}


def prev_period(p, cmp, grain):
    months, days = (12, 0) if cmp == "yoy" else _OFFSETS[grain]
    return _months_back_naive(p, months) if months else p - timedelta(days=days)


def shift_window(win, cmp, grain):
    """The documented rule, restated naively: shift the exclusive day
    bound so period ends map to period ends; datetimes keep time-of-day."""
    if win is None:
        return None
    months, days = (12, 0) if cmp == "yoy" else _OFFSETS[grain]
    lo, hi = win

    def back(x):
        if months:
            if isinstance(x, datetime):
                d2 = _months_back_naive(x.date() + timedelta(days=1), months) \
                     - timedelta(days=1)
                x = datetime.combine(d2, x.time())
            else:
                x = _months_back_naive(x, months)
        return x - timedelta(days=days) if days else x

    def back_lo(x):
        if months:
            if isinstance(x, datetime):
                x = datetime.combine(_months_back_naive(x.date(), months),
                                     x.time())
            else:
                x = _months_back_naive(x, months)
        return x - timedelta(days=days) if days else x

    # hi is inclusive at the surface: shift it via its exclusive day
    return (back_lo(lo), _shift_hi(hi, back))


def _shift_hi(hi, back):
    if isinstance(hi, datetime):
        return back(hi)
    return back(hi + timedelta(days=1)) - timedelta(days=1)


def fy_of(d, fys):
    d = _day(d)
    return d.year if d.month >= fys else d.year - 1


def compare_oracle(case, measures, by, ctx, win, grain, compare):
    """base map + {measure}_{cmp} columns, per the documented semantics."""
    base = slice_oracle(case, measures, by=by, ctx=ctx, win=win, grain=grain)
    out = {gk: dict(vals) for gk, vals in base.items()}
    for cmp in compare:
        if cmp == "fytd":
            cells = _fytd_cells(case, measures, by, ctx, win, grain, base)
        else:
            shifted = slice_oracle(case, measures, by=by, ctx=ctx,
                                   win=shift_window(win, cmp, grain),
                                   grain=grain)
            cells = {
                gk: shifted.get((prev_period(gk[0], cmp, grain),) + gk[1:])
                for gk in base
            }
        for gk in out:
            vals = cells.get(gk)
            for m in measures:
                out[gk][f"{m}_{cmp}"] = None if vals is None else vals[m]
    return out


def _fytd_cells(case, measures, by, ctx, win, grain, base):
    """Recomputed from base rows: same FY, period <= P, hi-truncated,
    same group. The window's LOW bound does not cut fytd."""
    ctx = dict(ctx or {})
    hi = win[1] if win else None
    rows = [r for r in case.rows
            if hi is None or in_window(r["d"], (date(1900, 1, 1), hi))]
    for key, op in ctx.items():
        rows = [r for r in rows if passes(_attr(case, r, key), op)]
    cells = {}
    for gk in base:
        p = gk[0]
        rs = [r for r in rows
              if fy_of(r["d"], case.fys) == fy_of(p, case.fys)
              and period_of(r["d"], grain, case.fys) <= p
              and tuple(_attr(case, r, b) for b in by) == gk[1:]]
        cells[gk] = {m: _agg(case.measures[m], rs) for m in measures}
    return cells
```

- [x] **Step 2: Add the differential test**

Append to `test_differential.py`:

```python
from oracle import compare_oracle


@pytest.mark.fuzz
@given(sc=scenarios(with_compare=True))
def test_compare_slices_match_the_oracle(sc):
    case, args = sc
    actual, _ = run_both(case, args)
    expected = compare_oracle(case, args.measures, args.by, args.ctx,
                              args.time, args.grain, args.compare)
    assert_maps_equal(actual, expected)
```

- [x] **Step 3: Run default + deep shakeout, then commit**

Run: `uv run pytest tests/metrics/test_differential.py -q`, then
`HYPOTHESIS_PROFILE=deep uv run pytest tests/metrics/test_differential.py -q`.

Expect real work here — compare semantics (partial-window truncation at
both ends, fytd FY-boundary behavior at week/fy_quarter grains, snapshot
per-lane as-at) are where the subtlety lives. The doc is the referee.

```bash
set -o pipefail; uv run pytest -q
git add tests/metrics/oracle.py tests/metrics/test_differential.py
git commit -m "Differential coverage for prior/yoy/fytd comparisons

The oracle restates the documented compare semantics naively (shifted
exclusive-day windows, fytd recomputed from base rows with FY equality);
randomized compare slices must match cell-exact, including gap periods
joining NULL and per-lane as-at on snapshot models."
```

---

## Task 6: Metamorphic properties

Spec-level consistency the oracle can't provide (both implementations
could share a wrong reading of the spec; these relations hold regardless).

**Files:**
- Create: `tests/metrics/test_properties.py`

- [x] **Step 1: Write the property tests**

```python
"""Metamorphic relations between the compiler's own answers. These hold
for ANY correct implementation of the documented semantics — they defend
against spec-level misreadings that differential testing cannot see."""

import duckdb
import pytest
from datetime import date, datetime, timedelta
from hypothesis import assume, given

from wh.metrics.compiler import compile_slice

from oracle import result_map, prev_period
from strategies import build_model, scenarios, seed, to_wh_context, Case, SliceArgs


def run(case, args):
    con = duckdb.connect()
    seed(con, case)
    compiled = compile_slice(
        build_model(case), list(args.measures), by=list(args.by),
        ctx=to_wh_context(args), grain=args.grain,
        compare=list(args.compare), suppress=args.suppress,
    )
    return result_map(
        con.execute(compiled.sql), args.by, args.measures, args.grain,
        compare=args.compare,
    )


ADDITIVE = ("n", "amt", "big")     # sum-family measures partition cleanly


@pytest.mark.fuzz
@given(sc=scenarios())
def test_by_partitions_the_total(sc):
    """Sum of by-group cells == the unfiltered total for additive
    measures. Standing tripwire for join fan-out and lane leakage."""
    case, args = sc
    measures = tuple(m for m in args.measures if m in ADDITIVE)
    assume(measures)
    args_by = SliceArgs(measures=measures, by=("facility.region",),
                        ctx=args.ctx, time=args.time, grain=args.grain)
    args_tot = SliceArgs(measures=measures, by=(), ctx=args.ctx,
                         time=args.time, grain=args.grain)
    parts, total = run(case, args_by), run(case, args_tot)
    for m in measures:
        by_period = {}
        for gk, vals in parts.items():
            p = gk[0] if args.grain else ()
            if vals[m] is not None:
                by_period[p] = by_period.get(p, 0) + vals[m]
        for gk, vals in total.items():
            p = gk[0] if args.grain else ()
            assert by_period.get(p, 0) == (vals[m] or 0), \
                f"{m} at {p}: groups sum to {by_period.get(p)} != {vals[m]}"


@pytest.mark.fuzz
@given(sc=scenarios())
def test_context_filter_equals_prefiltered_data(sc):
    """Filtering via context == deleting the non-matching rows first.
    Catches any filter leaking into the wrong lane."""
    from oracle import passes, _attr
    case, args = sc
    assume(args.ctx and not case.snapshot)   # as-at moves under row deletion
    kept = tuple(r for r in case.rows
                 if all(passes(_attr(case, r, k), op)
                        for k, op in args.ctx.items()))
    pre = Case(rows=kept, dim_rows=case.dim_rows, snapshot=False,
               fys=case.fys, measures=case.measures)
    args_nofilter = SliceArgs(measures=args.measures, by=args.by, ctx={},
                              time=args.time, grain=args.grain)
    assert run(case, args) == run(pre, args_nofilter)


@pytest.mark.fuzz
@given(sc=scenarios())
def test_adding_a_filter_never_increases_a_count(sc):
    case, args = sc
    measures = tuple(m for m in args.measures if m in ("n", "big", "ents"))
    assume(measures and "c" not in args.ctx)
    a = SliceArgs(measures=measures, by=args.by, ctx=args.ctx,
                  time=args.time, grain=args.grain)
    b = SliceArgs(measures=measures, by=args.by,
                  ctx={**args.ctx, "c": ("eq", "Cat 1")},
                  time=args.time, grain=args.grain)
    loose, tight = run(case, a), run(case, b)
    assert set(tight) <= set(loose)
    for gk, vals in tight.items():
        for m in measures:
            assert vals[m] <= loose[gk][m]


@pytest.mark.fuzz
@given(sc=scenarios())
def test_splitting_the_window_partitions_counts(sc):
    """[a, c] == [a, b] + [b+1, c] for sum-family measures. The standing
    tripwire for every inclusive/exclusive bound bug."""
    case, args = sc
    assume(args.time is not None and not case.snapshot)
    lo, hi = args.time
    assume(not isinstance(lo, datetime))
    assume((hi - lo).days >= 2)
    mid = lo + timedelta(days=(hi - lo).days // 2)
    measures = tuple(m for m in args.measures if m in ADDITIVE)
    assume(measures)
    whole = run(case, SliceArgs(measures=measures, by=args.by, ctx=args.ctx,
                                time=(lo, hi), grain=None))
    left = run(case, SliceArgs(measures=measures, by=args.by, ctx=args.ctx,
                               time=(lo, mid), grain=None))
    right = run(case, SliceArgs(measures=measures, by=args.by, ctx=args.ctx,
                                time=(mid + timedelta(days=1), hi), grain=None))
    for gk in set(whole) | set(left) | set(right):
        for m in measures:
            def val(r):
                return (r.get(gk) or {}).get(m) or 0
            assert val(left) + val(right) == val(whole), (gk, m)


@pytest.mark.fuzz
@given(sc=scenarios(with_compare=True))
def test_prior_equals_the_direct_value_of_the_previous_period(sc):
    """Spec-consistency: n_prior at P == n at prev(P) whenever the window
    is unbounded — no truncation, so the values must agree exactly."""
    case, args = sc
    assume(args.compare and "fytd" not in args.compare)
    args = SliceArgs(measures=args.measures, by=args.by, ctx=args.ctx,
                     time=None, grain=args.grain, compare=args.compare)
    res = run(case, args)
    for gk, vals in res.items():
        for cmp in args.compare:
            pk = (prev_period(gk[0], cmp, args.grain),) + gk[1:]
            for m in args.measures:
                direct = (res.get(pk) or {}).get(m)
                assert vals[f"{m}_{cmp}"] == direct, (gk, m, cmp)
```

- [x] **Step 2: Run default + deep, then commit**

Run: `uv run pytest tests/metrics/test_properties.py -q` then a deep pass.

```bash
set -o pipefail; uv run pytest -q
git add tests/metrics/test_properties.py
git commit -m "Metamorphic properties: partition sums, prefilter
equivalence, filter monotonicity, window splitting, prior==direct

Spec-level relations that hold for any correct implementation — they
defend against shared misreadings that differential testing cannot see."
```

---

## Task 7: Differential coverage for suppress= and complete_periods

**Files:**
- Modify: `tests/metrics/oracle.py`
- Modify: `tests/metrics/test_differential.py`

- [ ] **Step 1: Extend the oracle**

Append to `oracle.py`:

```python
def _cell_counts(case, by, ctx, win, grain):
    """Row count per cell — the count aggregate over the same lanes.
    The generator always includes the 'n' count spec."""
    assert case.measures["n"][0] == "count"
    return {gk: v["n"] for gk, v in
            slice_oracle(case, ("n",), by=by, ctx=ctx, win=win,
                         grain=grain).items()}


def apply_suppress(case, measures, by, ctx, win, grain, compare, result, k):
    """Cells under k rows go NULL — and each COMPARISON column is
    suppressed by its OWN lane's cell count (the shifted window's rows /
    the fytd cumulative rows), mirroring the compiler's per-CTE __cell_n.
    For the generated ratio spec the den is count(*) == the cell count,
    so the ratio's den<k rule coincides with the cell rule."""
    base_n = _cell_counts(case, by, ctx, win, grain)
    lane_n = {}
    for cmp in compare:
        if cmp == "fytd":
            base_map = slice_oracle(case, ("n",), by=by, ctx=ctx, win=win,
                                    grain=grain)
            lane_n[cmp] = {gk: v["n"] for gk, v in _fytd_cells(
                case, ("n",), by, ctx, win, grain, base_map).items()}
        else:
            shifted_n = _cell_counts(case, by, ctx,
                                     shift_window(win, cmp, grain), grain)
            lane_n[cmp] = {
                gk: shifted_n.get((prev_period(gk[0], cmp, grain),) + gk[1:])
                for gk in result
            }
    out = {}
    for gk, vals in result.items():
        out[gk] = {}
        for col, v in vals.items():
            cmp = next((c for c in compare if col.endswith(f"_{c}")), None)
            n = lane_n[cmp].get(gk) if cmp else base_n.get(gk, 0)
            out[gk][col] = None if (n is None or n < k) else v
    return out


def period_end_naive(p, grain):
    if grain == "day":
        return p
    if grain == "week":
        return p + timedelta(days=6)
    months = {"month": 1, "quarter": 3, "year": 12, "fy": 12, "fy_quarter": 3}[grain]
    y, m = divmod(p.year * 12 + p.month - 1 + months, 12)
    return date(y, m + 1, 1) - timedelta(days=1)


def complete_filter(case, result, grain, win):
    """Max-date rule (event models / no cadence): period end within data.
    Snapshot+cadence: final expected snapshot landed. Context-truncated
    periods are incomplete."""
    data_max = max(_day(r["d"]) for r in case.rows) if case.rows else None
    hi = _day(win[1]) if win else None
    out = {}
    for gk, vals in result.items():
        p = gk[0]
        pe = period_end_naive(p, grain)
        if case.snapshot and case.cadence == "weekly":
            in_p = [_day(r["d"]) for r in case.rows
                    if period_of(r["d"], grain, case.fys) == p]
            ok = bool(in_p) and max(in_p) > pe - timedelta(days=7)
        else:
            ok = data_max is not None and pe <= data_max
        if ok and hi is not None:
            ok = pe <= hi
        if ok:
            out[gk] = vals
    return out
```

NOTE: keep the generated ratio's den as `count(*)` so den == cell count
stays true; if a future spec adds a filtered den, extend `apply_suppress`
with a per-measure den count then. A missing prior period (join miss) is
NULL for both count and value reasons — `n is None` covers it and the
value was already None, so both implementations agree on that cell.

- [ ] **Step 2: Add the differential test**

Append to `test_differential.py`:

```python
from oracle import apply_suppress, complete_filter


@pytest.mark.fuzz
@given(sc=scenarios(with_compare=True, with_suppress=True, with_complete=True))
def test_suppress_and_complete_periods_match_the_oracle(sc):
    case, args = sc
    actual, _ = run_both(case, args)
    expected = compare_oracle(case, args.measures, args.by, args.ctx,
                              args.time, args.grain, args.compare)
    if args.complete_periods:
        expected = complete_filter(case, expected, args.grain, args.time)
    if args.suppress is not None:
        expected = apply_suppress(case, args.measures, args.by, args.ctx,
                                  args.time, args.grain, args.compare,
                                  expected, args.suppress)
    assert_maps_equal(actual, expected)
```

The oracle asserts the per-lane rule: each comparison column suppressed
by its own lane's cell count (`__cmp_*.__cell_n`), never the base
cell's. If the deep run disagrees, first check `docs/metrics.md`'s
suppress note actually documents which lane's count governs a comparison
column — if it's silent, that ambiguity is itself a finding: fix the doc
in the same commit as whichever side changes.

- [ ] **Step 3: Run default + deep, resolve, commit**

```bash
set -o pipefail; uv run pytest -q
git add tests/metrics/oracle.py tests/metrics/test_differential.py docs/metrics.md
git commit -m "Differential coverage for suppress= and complete_periods

Suppression thresholds and completeness rules recomputed independently in
the oracle; randomized slices with both flags (plus compare) must match
cell-exact, including which lane's cell count governs comparison columns."
```

---

## Task 8: Totality fuzzing of the trust boundary

**Files:**
- Create: `tests/metrics/test_fuzz_loader.py`

- [ ] **Step 1: Write the fuzz tests**

```python
"""The loader is the firewall: every input either loads to Models or
raises SemanticsError — never a crash, never a silent partial load. And
generated SQL always parses, whatever a context value contains."""

import duckdb
import pytest
import yaml
from hypothesis import given, note, strategies as st

from wh.errors import SemanticsError, WhError
from wh.metrics.compiler import compile_slice
from wh.metrics.loader import load_definitions

from strategies import Case, MEASURES, SliceArgs, build_model, render_yaml, seed, to_wh_context, cases

_keys = st.text(min_size=0, max_size=20)
_scalars = st.one_of(st.none(), st.booleans(), st.integers(),
                     st.floats(allow_nan=False), st.text(max_size=20))
_docs = st.recursive(
    _scalars,
    lambda c: st.one_of(st.dictionaries(_keys, c, max_size=4),
                        st.lists(c, max_size=4)),
    max_leaves=25,
)


@pytest.mark.fuzz
@given(doc=_docs)
def test_loader_is_total_on_arbitrary_yaml(doc, tmp_path_factory):
    d = tmp_path_factory.mktemp("fuzz")
    try:
        (d / "f.yml").write_text(yaml.safe_dump(doc, allow_unicode=True))
    except yaml.YAMLError:
        return                              # not representable — fine
    try:
        models = load_definitions(d, fiscal_year_start=7)
        assert isinstance(models, dict)
    except SemanticsError:
        pass                                # the only acceptable failure


@pytest.mark.fuzz
@given(case=cases(), data=st.data())
def test_loader_is_total_under_structure_aware_mutation(case, data, tmp_path_factory):
    """Take a VALID definition, break it one edit at a time: the result
    must load or raise SemanticsError — silent acceptance of a broken
    definition is the failure mode that matters."""
    doc = yaml.safe_load(render_yaml(case))
    path = []
    node = doc
    while isinstance(node, dict) and node and data.draw(st.booleans()):
        key = data.draw(st.sampled_from(sorted(node)))
        path.append(key)
        node = node[key]
    mutation = data.draw(st.sampled_from(["typo_key", "wrong_type", "delete"]))
    target = doc
    for key in path[:-1]:
        target = target[key]
    key = path[-1] if path else data.draw(st.sampled_from(sorted(doc)))
    if mutation == "typo_key":
        target[key + "x"] = target.pop(key)
    elif mutation == "wrong_type":
        target[key] = [target[key]]
    else:
        del target[key]
    d = tmp_path_factory.mktemp("mut")
    (d / "f.yml").write_text(yaml.safe_dump(doc))
    note(yaml.safe_dump(doc))
    try:
        load_definitions(d, fiscal_year_start=case.fys)
    except SemanticsError:
        pass


@pytest.mark.fuzz
@given(value=st.text(max_size=40))
def test_context_values_never_break_the_generated_sql(value):
    """Any string context value: SQL parses, executes, and the output
    column set is exactly the declared namespace — quotes, unicode,
    SQL-looking text included."""
    from datetime import date
    case = Case(
        rows=({"d": date(2026, 1, 1), "k": "U1", "c": value, "v": 1, "fk": "K1"},),
        dim_rows=(("K1", "North"), ("K2", "South"), ("K3", "North")),
        snapshot=False, fys=7, measures=dict(MEASURES),
    )
    con = duckdb.connect()
    seed(con, case)
    compiled = compile_slice(
        build_model(case), ["n"],
        ctx=to_wh_context(SliceArgs(measures=("n",), by=(),
                                    ctx={"c": ("eq", value)}, time=None,
                                    grain=None)),
    )
    res = con.execute(compiled.sql)
    assert [d[0] for d in res.description] == ["n"]
    assert res.fetchall() == [(1,)]
```

- [ ] **Step 2: Run default + deep, fix findings, commit**

Any non-`SemanticsError` exception out of the loader is a real finding —
fix it in `loader.py` (wrap or check), don't catch it in the test.

```bash
set -o pipefail; uv run pytest -q
git add tests/metrics/test_fuzz_loader.py src/wh/metrics/loader.py
git commit -m "Totality fuzzing: loader never fails open or crashes

Arbitrary YAML and structure-aware single-edit mutations of valid
definitions must load or raise SemanticsError; arbitrary context strings
must compile to parseable SQL with exactly the declared output columns."
```

---

## Task 9: Mutation testing — measure that the suite bites

**Files:**
- Modify: `pyproject.toml`
- Modify: `docs/testing.md`
- Possibly new tests for surviving mutants

- [ ] **Step 1: Configure mutmut**

Add to `pyproject.toml` (adjust to the installed mutmut major version —
verify with `uv run mutmut --help` and its docs before trusting the shape):

```toml
[tool.mutmut]
paths_to_mutate = "src/wh/metrics/"
tests_dir = "tests/metrics/"
```

Set the runner to exclude generative tests for speed (they're covered by
their own layers): the run command is
`uv run mutmut run` with the pytest invocation configured (or defaulted)
to `python -m pytest -x -q tests/metrics -m "not fuzz"`.

- [ ] **Step 2: Baseline the small modules first**

Run mutation on `timegrain.py` and `context_ops.py` (fast: small files,
6s suite). Record per-module: mutants generated / killed / survived.

- [ ] **Step 3: Run `compiler.py` in the background**

This is hours of wall-clock (hundreds of mutants × the suite). Start it
in the background and continue; collect results when done.

- [ ] **Step 4: Triage every survivor**

For each surviving mutant, one of exactly two outcomes:
1. **A test gap** — write the test that kills it (this is the payoff;
   expect bound-flip and operator-swap survivors to point at untested
   interactions), or
2. **An equivalent mutant** (behavior genuinely unchanged) — record it as
   such in `docs/testing.md` with one line of reasoning.

No third bucket. "Probably fine" is a test gap.

- [ ] **Step 5: Record the baseline and commit**

Fill in `docs/testing.md`'s Mutation section: date, per-module scores,
the survivor ledger, and the exact command to re-run.

```bash
set -o pipefail; uv run pytest -q
git add pyproject.toml docs/testing.md tests/metrics/
git commit -m "Mutation-testing baseline for the metrics layer

Recorded per-module kill scores, killed every triaged survivor with a new
test or documented it as equivalent. The score is the standing answer to
'would the suite notice if this code were wrong?'"
```

---

## Task 10: Engine matrix, traceability, CLAUDE.md, final gate

**Files:**
- Create: `scripts/engine_matrix.sh`
- Modify: `docs/testing.md`, `CLAUDE.md`

- [ ] **Step 1: Engine matrix script**

```bash
#!/usr/bin/env bash
# The emitted SQL means what DuckDB says it means — run the metrics suite
# against the pinned engine and the latest release before upgrading.
set -euo pipefail
cd "$(dirname "$0")/.."
pinned=$(uv run python -c 'import duckdb; print(duckdb.__version__)')
for spec in "duckdb==${pinned}" "duckdb"; do
    echo "=== ${spec}"
    uv run --with "${spec}" -- pytest tests/metrics -q
done
```

`chmod +x scripts/engine_matrix.sh`; run it once and confirm both legs
pass (the second leg may legitimately fail on a future DuckDB — that's
the script doing its job; note it rather than pinning around it).

- [ ] **Step 2: Traceability table in `docs/testing.md`**

Fill in the guarantee map — every documented guarantee names its tests:

```markdown
| Guarantee (docs/metrics.md) | Tests |
|---|---|
| Two-lane isolation (intrinsic FILTER vs context WHERE) | test_invariants.py (canaries), test_differential.py |
| As-at never moves under attribute context | test_invariants.py, oracle `_asat_filter` differential |
| Day-inclusive time ranges, exact datetime bounds | test_compiler.py::test_time_context_is_day_inclusive_on_both_ends, test_properties.py::test_splitting_the_window_partitions_counts |
| Compare: shifted windows, period-end mapping, gaps stay NULL | test_compare.py, test_differential.py::test_compare_slices_match_the_oracle, test_properties.py::test_prior_equals_the_direct_value_of_the_previous_period |
| fytd recomputed from base, FY-bounded, group-isolated | test_compare.py::test_fytd_*, oracle `_fytd_cells` differential |
| Suppression: cell rows under n go NULL; ratio den rule | test_suppress.py, test_differential.py::test_suppress_and_complete_periods_match_the_oracle |
| Loader firewall: identifiers validated, strict keys, totality | test_loader.py, test_fuzz_loader.py |
| Output namespace: every column named exactly once | test_compiler.py::test_duplicate_output_columns_error, fuzz column-set asserts |
| Hash stability across engine upgrades | test_provenance.py, scripts/engine_matrix.sh |
```

(Verify each row's test names against the actual suite while writing —
the table is only defensible if grep confirms it.)

- [ ] **Step 3: CLAUDE.md updates**

Add to Commands:
- `HYPOTHESIS_PROFILE=deep uv run pytest tests/metrics -m fuzz` — deep generative run
- `uv run mutmut run` — mutation testing (see docs/testing.md)
- `bash scripts/engine_matrix.sh` — suite against pinned + latest DuckDB

Add to the metrics notes:
- `tests/metrics/oracle.py` must never import `wh.*` — its independence
  from the compiler is what makes differential agreement evidence; the
  duplication with timegrain/compiler is deliberate.
- New compiler features are not done until they have an oracle
  interpretation (differential) or an explicit metamorphic property —
  structural tests alone don't count as coverage for semantics.
- Current state entry: test-harness plan executed
  (docs/plans/2026-07-21-metrics-test-harness.md) with the layers and
  where the mutation baseline lives.

- [ ] **Step 4: Final gate and push**

```bash
set -o pipefail
uv run pytest -q
HYPOTHESIS_PROFILE=deep uv run pytest tests/metrics -m fuzz -q
git add scripts/ docs/testing.md CLAUDE.md
git commit -m "Engine matrix script, guarantee traceability, harness docs"
git push -u origin claude/semantic-compiler-review-jdv33t
```

---

## Self-review notes

- **Coverage vs the recommendation:** oracle+differential → Tasks 2–5, 7;
  metamorphic → Task 6; mutation → Task 9; totality fuzz → Task 8; engine
  matrix + traceability + process → Task 10. Layer-validation ("prove the
  harness bites before trusting green") is built into Tasks 3 (comparator
  self-tests) and 4 (planted-bug meta-test).
- **Known-risky code in this plan:** oracle compare/fytd/suppress
  semantics are restatements of subtle contracts — Tasks 5 and 7
  explicitly budget a deep-run shakeout and name the doc as referee; the
  mutmut config shape must be verified against the installed version
  (Task 9 Step 1 says so). These are verification steps, not gaps.
- **Determinism:** Hypothesis manages seeds and its example database
  (`.hypothesis/`, gitignored); failures reproduce via the printed
  blob/seed. No `Date.now`-style nondeterminism is introduced.
- **Fast suite preserved:** all generative tests are `-m fuzz` with a
  40-example default profile; the plain `uv run pytest` gate stays in
  seconds. Deep budgets are explicit and documented.
