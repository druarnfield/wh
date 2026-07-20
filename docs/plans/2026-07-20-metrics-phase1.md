# Metrics Layer Phase 1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. NO worktree — work on `greenfield-metrics` in the main checkout.

**Goal:** Remove the BSL integration (rollout step 0) and build the home-grown metrics layer's core (rollout step 1 of `docs/plans/2026-07-20-metrics-design.md`): model loading + validation, the context object, and single-fact `slice()` with two-lane compilation, grains (incl. fiscal), snapshot row-selection, `time_agg`, and ratios — with the structural/invariant test suite.

**Architecture:** New package `src/wh/metrics/` (package, not `metrics.py` — the compiler is too big for one module; no submodule may be named `model`/`slice`/`context` — verb-shadowing gotcha). Pure-function core: YAML → frozen dataclasses → SQL string; DuckDB only at the edges (bind-time checks, execution, `json_serialize_sql` for tests). The two lanes are the compilation shape: intrinsic `where` compiles to `FILTER (WHERE ...)` per measure, context compiles to the outer `WHERE`, and on snapshot models the as-at subquery contains time predicates only.

**Tech Stack:** DuckDB (dialect commitment: `GROUP BY ALL`, `FILTER`, `json_serialize_sql`), PyYAML, pytest. No BSL, no ibis.

**Out of scope (later rollout steps):** `compare=`, `complete_periods`, `.suppress()` (step 2); provenance + hashing (step 3); marimo widgets beyond context resolution (step 4).

---

## Task 1: Remove the BSL integration (rollout step 0)

Deletion, not TDD. Keep `errors.SemanticsError` (the new layer uses it) and `config.semantics_dir` (same config key). Keep `docs/upstream/` and the `to_pyarrow` duck-typing branch in `frames.to_arrow` (generic, no BSL dependency).

**Files:**
- Delete: `src/wh/semantics.py`, `tests/test_semantics.py`
- Modify: `src/wh/__init__.py` — remove `models`/`model`/`frame` functions and their `__all__` entries
- Modify: `src/wh/workspace.py` — remove `_ibis()`, `models()`, `model()`, `frame()` methods and the `_ibis_cache`/`_models_cache` attrs (lines 39–40)
- Modify: `src/wh/cli.py:34-38` — remove the `validate_semantics` import/call/print
- Modify: `tests/test_cli.py` — remove the three `validate_semantics_*` tests; keep `test_validate_no_semantics_dir_still_ok` if it passes without the hook, else drop
- Modify: `tests/conftest.py` — remove the `semantic_project` fixture (grep for it)
- Modify: `pyproject.toml` — remove the `[semantics]` extra and the `boring-semantic-layer` dev dependency; run `uv sync`
- Modify: `README.md` — replace the semantic-layer sections (~lines 37, 74–75, 127–161) with a one-paragraph "metrics layer: in development, see docs/plans/2026-07-20-metrics-design.md"; keep the marimo variable-panel note only if it survives as generally useful, else drop
- Modify: `CLAUDE.md` — delete the "Semantics notes" section and the BSL bullets in Current state; add "BSL removed (2026-07-20)"

**Steps:** delete/edit as above → `uv sync` → `uv run pytest` (all green, no skips beyond the usual integration auto-skips) → `git grep -i "bsl\|boring_semantic\|ibis"` returns only docs/ hits → commit `refactor: remove BSL integration (metrics design step 0)`.

---

## Task 2: `fiscal_year_start` config key

**Files:** Modify `src/wh/config.py` (Config dataclass + `load_config`), Test `tests/test_config_load.py`

**Step 1 — failing tests** (append to `tests/test_config_load.py`, mirroring its existing style):

```python
def test_fiscal_year_start_default_is_calendar(tmp_path):
    cfg = _write_and_load(tmp_path, MINIMAL_YAML)          # use the file's existing helper/pattern
    assert cfg.fiscal_year_start == 1

def test_fiscal_year_start_parsed(tmp_path):
    cfg = _write_and_load(tmp_path, MINIMAL_YAML + "\nsemantics:\n  fiscal_year_start: 7\n")
    assert cfg.fiscal_year_start == 7

@pytest.mark.parametrize("bad", [0, 13, "july"])
def test_fiscal_year_start_invalid(tmp_path, bad):
    with pytest.raises(ConfigError, match="fiscal_year_start"):
        _write_and_load(tmp_path, MINIMAL_YAML + f"\nsemantics:\n  fiscal_year_start: {bad}\n")
```

**Step 2:** run → FAIL (`Config` has no field). **Step 3:** add `fiscal_year_start: int = 1` to `Config`; in `load_config`, read `(raw.get("semantics") or {}).get("fiscal_year_start", 1)`, require `isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 12` else `ConfigError("semantics.fiscal_year_start must be an integer 1-12 (got …)")`. **Step 4:** pass. **Step 5:** commit `feat: fiscal_year_start config for metrics layer`.

---

## Task 3: metrics package + shared-dimension loading

**Files:**
- Create: `src/wh/metrics/__init__.py`, `src/wh/metrics/loader.py`
- Create: `tests/metrics/__init__.py` (empty), `tests/metrics/conftest.py`, `tests/metrics/test_loader.py`

**Data model** (in `loader.py`, frozen dataclasses):

```python
@dataclass(frozen=True)
class SharedDim:
    name: str; table: str; key_column: str
    attributes: dict[str, str]          # attr -> dim-table column
    hierarchy: tuple[str, ...] = ()

@dataclass(frozen=True)
class Measure:
    name: str; description: str
    expr: str | None = None             # DuckDB aggregate SQL
    where: str | None = None            # intrinsic predicate (fact cols only)
    ratio: tuple[str, str] | None = None  # (num_expr, den_expr)
    time_agg: str = "sum"               # sum | last | avg | none
    additive: bool = True               # effective flag (see Task 4)

@dataclass(frozen=True)
class DimRef:
    shared: SharedDim | None            # None = local/degenerate dim
    fact_column: str                    # fact-side key (shared) or the fact column itself (local)

@dataclass(frozen=True)
class Model:
    name: str; fact: str; description: str
    time_column: str; cadence: str | None; snapshot: bool
    dims: dict[str, DimRef]; measures: dict[str, Measure]
    strict_context: bool; fiscal_year_start: int
```

Public loader API: `load_definitions(directory: Path, fiscal_year_start: int) -> dict[str, Model]` — merges `*.yml|*.yaml` (sorted), reserved top-level key `dimensions:` = shared dims, every other top-level key is a model. All errors are `SemanticsError` with file name + the fix, per the notebook-friendly-errors convention.

**conftest.py fixture** `write_defs(tmp_path)` helper: writes given YAML strings to `tmp_path/"semantics"` and calls `load_definitions`. Also a canonical `DESIGN_YAML` constant: the full example from the design doc (facility/doctor dims, waitlist + removals models).

**Step 1 — failing tests:** shared dim parsed with attrs + hierarchy tuple; two files merge; same dim defined in two files → `SemanticsError` naming both files; same model in two files → error; hierarchy naming an undeclared attribute → error ("hierarchy entry 'region' is not a declared attribute of dimension 'facility'"); `dimensions` entry missing `table`/`key_column` → error. **Steps 2–5:** verify fail → implement (port the merge loop shape from the old `merge_model_files`, without the BSL lint) → pass → commit `feat(metrics): shared-dimension loading`.

## Task 4: model + measure parsing and load-time rules

**Files:** Modify `src/wh/metrics/loader.py`, Test `tests/metrics/test_loader.py`

**Step 1 — failing tests**, one per design rule:
- `DESIGN_YAML` loads: `waitlist.snapshot is True`, dims resolved (`dims["facility"].shared.table == "main.clinic_dim"`, `dims["urgency"].shared is None`), measures parsed incl. ratio tuple.
- measure without `description` → error quoting the rule ("an auditable definition without prose isn't one" spirit; message: "measure 'x' needs a description").
- model without `fact:` or without `time.column` → error.
- local dim name shadowing a shared dim → error naming both.
- **default `time_agg`:** `last` when `snapshot: true`, `sum` otherwise; explicit value wins.
- `time_agg: sum` on a snapshot model → error whose message names the fix ("model the flow as its own event-grain fact").
- invalid `time_agg` value → error listing the four.
- ratio measure with only `num` → error.
- **effective additivity:** `additive` explicitly false, OR ratio, OR `expr` matches `DISTINCT`/`median(`/`mode(` (case-insensitive regex) → `additive=False`; plain `count(*)`/`sum(...)` stays True.

**Steps 2–5:** fail → implement `_parse_model`/`_parse_measure` → pass → commit `feat(metrics): model parsing with load-time rules`.

---

## Task 5: context object and op vocabulary

**Files:** Create `src/wh/metrics/context_ops.py` (NOT `context.py` — the module would collide with the `wh.context` verb re-exported from the package), Test `tests/metrics/test_context.py`

Ops are tiny frozen dataclasses: `Eq(value)`, `In(values: tuple)`, `Not(value)`, `Between(lo, hi)` (inclusive both ends), `LastPeriods(n, unit)`, `All()`. Public helpers `not_`, `last`, `all_` (exported as `wh.not_`, `wh.last`, `wh.all`). `Context` is an immutable mapping `key -> op-or-widget`; key syntax `dimension__attribute`, plus reserved `time`.

Value coercion in `context(**kw)`: scalar → `Eq`; list/tuple of scalars on a non-`time` key → `In`; `time=(a, b)` → `Between`; op instances pass through; objects with a `.value` attribute (widgets) are stored unresolved.

**Step 1 — failing tests:**
```python
def test_context_basics_and_immutability():
    ctx = wh.context(facility__region="North", doctor__specialty=["ENT", "Ophthal"])
    assert ctx.entries["facility__region"] == Eq("North")
    ctx2 = ctx.with_(facility__district="Coastal")
    assert "facility__district" not in ctx.entries and "facility__district" in ctx2.entries
    assert ctx.without("doctor__specialty").entries.keys() == {"facility__region"}

def test_merge_right_wins_per_attribute():
    merged = wh.context(facility__region="North") | wh.context(facility__region="South")
    assert merged.entries["facility__region"] == Eq("South")

def test_literal_empty_list_is_an_error():
    with pytest.raises(SemanticsError, match="wh.all"):   # error text teaches the widget/wh.all() fix
        wh.context(facility__region=[])

class FakeWidget:  # anything with .value — marimo duck-type
    def __init__(self, v): self.value = v

def test_widget_resolution_at_resolve_time():
    w = FakeWidget(["ENT"])
    ctx = wh.context(doctor__specialty=w)
    w.value = ["ENT", "Ophthal"]                       # changed after construction
    assert ctx.resolve(anchor=date(2026, 7, 19)).entries["doctor__specialty"] == In(("ENT", "Ophthal"))

def test_empty_widget_means_unfiltered():
    r = wh.context(doctor__specialty=FakeWidget([])).resolve(anchor=date(2026, 7, 19))
    assert r.entries["doctor__specialty"] == All()

def test_relative_time_resolves_against_anchor_not_wall_clock():
    r = wh.context(time=wh.last(12, "month")).resolve(anchor=date(2026, 7, 14))
    assert r.entries["time"] == Between(date(2025, 7, 15), date(2026, 7, 14))

def test_resolved_context_is_plain_data():
    r = wh.context(facility__region="North").resolve(anchor=date(2026, 7, 19))
    assert r.is_resolved and isinstance(hash(r), int) and r.to_dict() == {"facility__region": {"eq": "North"}}

def test_unresolved_context_refuses_hash_and_to_dict():
    ctx = wh.context(doctor__specialty=FakeWidget(["ENT"]))
    with pytest.raises(SemanticsError, match="resolve"):
        ctx.to_dict()
```
**Steps 2–5:** fail → implement → pass → commit `feat(metrics): context object with op vocabulary and resolution`.

## Task 6: time grain expressions (incl. fiscal)

**Files:** Create `src/wh/metrics/timegrain.py`, Test `tests/metrics/test_timegrain.py`

`grain_expr(grain: str, column_sql: str, fiscal_year_start: int) -> str` returning a period-**start** date expression. Grains v1: `day, week, month, quarter, year, fy, fy_quarter`. Fiscal via the shift trick: `date_trunc('year', {col} - INTERVAL {f-1} MONTH) + INTERVAL {f-1} MONTH` (same with `'quarter'` for `fy_quarter`); when `fiscal_year_start == 1`, `fy`/`fy_quarter` degrade to plain `year`/`quarter`. Unknown grain → `SemanticsError` listing valid grains.

**Step 1 — failing tests:** evaluate each expression in a bare `duckdb.connect()` over literal dates and assert period starts — the fiscal cases from the design: f=7, `2025-08-15 → fy 2025-07-01`, `2025-03-10 → fy 2024-07-01`, `2025-08-15 → fy_quarter 2025-07-01`, `2025-11-02 → fy_quarter 2025-10-01`; f=1 equals `date_trunc`. **Steps 2–5** → commit `feat(metrics): time grain expressions incl. fiscal`.

---

## Task 7: compiler — basic slice (lanes, joins, GROUP BY ALL)

**Files:** Create `src/wh/metrics/compiler.py`, `tests/metrics/test_compiler.py`; extend `tests/metrics/conftest.py`

**conftest additions:** a `con` fixture (in-memory DuckDB) seeding:
- `main.clinic_dim(clinic_code, clinic_name, hospital_name, district, region)` — 4 clinics, 2 regions;
- `main.doctor_dim(doctor_id, specialty, seniority_band)`;
- `main.waitlist(snapshot_date DATE, clinic_code, doctor_id, urgency_category, ur, wait_days INT, target_days INT)` — weekly snapshots spanning ≥ 2 months, incl. one clinic whose final-month snapshot lags (for Task 10);
- `main.waitlist_removals(removal_date DATE, clinic_code, doctor_id, removal_reason)` — incl. `'ADMIN'` rows.

Also `assert_sql_equiv(con, actual_sql, expected_sql)`: both through `SELECT json_serialize_sql($$ … $$)`, `json.loads`, assert equal trees. Formatting differences vanish; semantic drift fails.

**Compiler API:**
```python
def compile_slice(model, measures: list[str], by: list[str], resolved_ctx, grain: str | None) -> str
```
Shape rules (all structurally tested):
- `FROM {fact} AS fact`; `LEFT JOIN {dim.table} AS {name} ON fact.{fact_column} = {name}.{key_column}` — **only** for dims referenced by `by` or context (local dims never join).
- select list: `[grain_expr AS period]` + by attributes as `{dim}.{col} AS "{dim}.{attr}"` (local: `fact.{col} AS "{name}"`) + measures: `{expr} AS {name}` with intrinsic where as `{agg} FILTER (WHERE {where})` — merging into an authored FILTER is out of scope v1: `expr` + `where` compile as `CASE`-free `FILTER`; an expr that already contains FILTER plus a `where:` key → load error (Task 4 addendum: add that rule + test here if not already).
- context lane: outer `WHERE`, one predicate per applied entry — `Eq` → `=`, `In` → `IN (...)`, `Not` → `IS DISTINCT FROM`, `Between` on `time` → `fact.{time_column} BETWEEN DATE 'a' AND DATE 'b'`, `All` → omitted. String literals escaped (`'` doubled); dates rendered `DATE '...'`. NULL never matches membership tests (plain `IN` semantics) per design.
- `GROUP BY ALL ORDER BY period` (ORDER BY only when grain present).

**Step 1 — failing structural tests** (removals model, no snapshot complexity):
```python
def test_lanes_and_pruning(con, defs):
    sql = compile_slice(defs["removals"], ["removals"], by=["facility.region"],
                        resolved_ctx=resolve(wh.context(facility__region="North")), grain="month")
    assert_sql_equiv(con, sql, """
        SELECT date_trunc('month', fact.removal_date) AS period,
               facility.region AS "facility.region",
               count(*) FILTER (WHERE removal_reason <> 'ADMIN') AS removals
        FROM main.waitlist_removals AS fact
        LEFT JOIN main.clinic_dim AS facility ON fact.clinic_code = facility.clinic_code
        WHERE facility.region = 'North'
        GROUP BY ALL ORDER BY period
    """)

def test_no_join_when_dim_unreferenced(con, defs):
    sql = compile_slice(defs["removals"], ["removals"], by=[], resolved_ctx=EMPTY, grain=None)
    assert "JOIN" not in sql.upper()          # coarse guard; structural test covers the rest
```
Plus: unknown measure/by-attribute → `SemanticsError` naming the declared surface; a by= on a local dim compiles from the fact column. **Steps 2–5** → commit `feat(metrics): single-fact compiler with two lanes and join pruning`.

## Task 8: canary lane-invariant tests

**Files:** Create `tests/metrics/test_invariants.py` (+ helper in conftest)

Helper `where_subtree(con, sql)`: `json_serialize_sql` the query, return (serialized outer-`WHERE` subtree JSON, whole tree JSON). Invariant test: compile with canary context values (`facility__region="__CANARY_ATTR__"`, `time=(date(1893,1,7), date(1893,1,8))`); assert `"__CANARY_ATTR__"` occurs in the whole tree **exactly as many times as in the outer-WHERE subtree** (i.e. nowhere else), and occurs at least once. Run over a grid: measures with/without intrinsic `where`, ratio measures, with/without `by`. Commit `test(metrics): canary lane invariants`.

## Task 9: ratio measures

**Files:** Modify `src/wh/metrics/compiler.py`, Test `tests/metrics/test_compiler.py` + behavioural in `test_slice_behaviour.py`

When any requested measure is a ratio: inner select (the Task-7 shape) computes `{num} AS __{name}_num`, `{den} AS __{name}_den` (each with intrinsic-where FILTER if declared); outer select projects group cols, plain measures by name, and `CAST(__{name}_num AS DOUBLE) / NULLIF(__{name}_den, 0) AS {name}` — components not projected outward. No ratio requested → no wrapper (Task 7 tests must keep passing unchanged).

**Tests:** structural (division only in outermost select; `__num`/`__den` beneath); tree-walk property: no `/` operator anywhere except the outer select; behavioural: `pct_over_target` by region equals a hand-written SQL result at unequal group sizes (the classic avg-of-ratios trap). Commit `feat(metrics): ratio measures compile as num/den + outer division`.

## Task 10: snapshot as-at row selection

**Files:** Modify `src/wh/metrics/compiler.py`, Tests in `test_compiler.py`, `test_invariants.py`, `test_slice_behaviour.py`

On `snapshot: true`, insert between FROM and the dim joins:
```sql
JOIN (
  SELECT {grain_expr over snapshot col} AS __period, max({snapshot col}) AS __as_at
  FROM {fact}
  WHERE {time-context predicates ONLY}
  GROUP BY 1
) AS __asat
  ON {grain_expr over fact.{snapshot col}} = __asat.__period AND fact.{snapshot col} = __asat.__as_at
```
(no grain → ungrouped `max` subquery, join on the `__as_at` equality alone). The outer WHERE still carries **all** context entries including time.

**Tests:**
- invariant (extend Task 8): time canaries may appear in the `__asat` subquery; attribute canaries must never — assert attribute-canary count in whole tree == count in outer WHERE subtree, and time-canary appearances occur only in outer WHERE + `__asat` subtree.
- behavioural: monthly `patients_waiting` = distinct `ur` in the month's **final** snapshot, not a sum over snapshots; `median_wait` at `grain="month"` = median over final-snapshot rows only;
- **global-max fixture:** the lagging clinic (absent from the period's final snapshot) contributes nothing to that period row, and a `facility__region` filter on the lagging region returns the same as-at rows as the unfiltered slice restricted to that region (one moment, both slices).

Commit `feat(metrics): snapshot global-max as-at row selection`.

## Task 11: slice-time errors, miss rule, strict context

**Files:** Create `src/wh/metrics/result.py` (the `Slice` object; construction = compile, execution lazy), Test `tests/metrics/test_slice_behaviour.py`

`Slice` fields: `.sql` (str), `.applied` / `.ignored` (context keys — the miss-rule bookkeeping that provenance later consumes), `.frame(backend=None)`, `.view(name)`. Miss rule: an entry applies iff its dimension resolves on this model **by shared-dim identity** (or local-dim name); otherwise it lands in `.ignored`. `strict_context` (model-level default, per-slice override) turns non-empty `.ignored` into a `SemanticsError` naming the ignored keys and the model's declared surface.

**Tests:** ignored entry recorded, applied unaffected; strict raises; model-level `strict_context: true` honoured; per-slice `strict_context=False` overrides it; unknown grain/measure error messages name the rule. Commit `feat(metrics): Slice result with miss rule and strict context`.

## Task 12: lane-isolation property + drill consistency (behavioural)

**Files:** Test `tests/metrics/test_slice_behaviour.py`

- **Lane isolation:** for a grid of contexts (empty / region Eq / specialty In / time Between / combinations), `long_waiters` (intrinsic `where`) computed by the layer equals a hand-written query applying the same outer predicates with the intrinsic predicate hardcoded in FILTER — same in-scope rows, both ways, exact equality.
- **Drill consistency:** for additive `removals`, region totals == sum of district-level slices under the same context (LEFT-JOIN NULL group included in the reconciliation).

Commit `test(metrics): lane-isolation property and drill consistency`.

## Task 13: bind-time integrity checks

**Files:** Create `src/wh/metrics/checks.py`, Test `tests/metrics/test_checks.py`

`bind_checks(con, model) -> list[str]` (warnings returned; errors raised):
1. **EXPLAIN validation:** compile a representative full SELECT (all measures, all dims in `by`, no context) and `EXPLAIN` it — any DuckDB error becomes `SemanticsError` with model name + the DuckDB message.
2. **Intrinsic-where fact-columns-only:** `json_serialize_sql('SELECT 1 FROM {fact} WHERE {where}')`, walk for `COLUMN_REF` nodes; any multi-part reference or name not in the fact's schema (from `DESCRIBE`) → `SemanticsError` naming the rule ("intrinsic predicates may reference fact columns only — identity lives in git, dimension state does not").
3. **Dim-key uniqueness:** `count(*) != count(DISTINCT key)` on each referenced shared dim table → `SemanticsError` naming the table (join fan-out inflates every non-distinct measure).
4. **Orphan keys:** per fact→dim key, count fact keys missing from the dim → warning string "facility: N orphan keys, P%" (stored for provenance later).

Results cached per `(id(con), fact table, max _mirror.meta.extracted_at if present)` — self-keying, no invalidation hooks. **Tests:** each check with a deliberately broken fixture (dup dim key errors naming table; intrinsic where referencing `facility.region` errors naming the rule; orphan fixture warns and the NULL group appears under `by=`). Commit `feat(metrics): bind-time integrity checks`.

## Task 14: workspace + module wiring, mtime reload

**Files:** Modify `src/wh/workspace.py`, `src/wh/__init__.py`, `src/wh/metrics/__init__.py`, Test `tests/metrics/test_wiring.py`

- `Workspace.model(name)` → bound model handle `BoundModel(model, ws)` with `.slice(measures=[], by=[], context=None, grain=None, strict_context=None)`; runs `bind_checks` on first bind. Cache self-keys on `(ws.con identity, tuple of (yaml path, mtime))` — mtime bump = auto-reload; never an invalidation hook.
- Module verbs in `wh/__init__.py`: `model(name)`, `slice(model_name, **kw)` (sugar), `context(**kw)`, `not_`, `last`, `all_` as `wh.all` — pure re-exports/delegates; **eager-import note:** `metrics` is a package whose name collides with no verb, but re-check `test_module_verbs_survive_submodule_imports` still passes with the new imports.
- `Slice.frame()` executes on `ws.con`, converts via `frames.from_arrow(..., ws._backend(backend))`; `.view(name)` = `CREATE OR REPLACE VIEW` on `ws.con`.
- Anchor for `Context.resolve`: `max(fact time column)` via one cheap query at slice time (design: mirror data, not wall clock).

**Tests:** `wh.model("waitlist").slice(...)` end-to-end on a tmp workspace (reuse `tests/conftest.py` project fixture pattern + seeded duckdb file); YAML edit + mtime bump → new definition picked up; verb-survival test still green. Commit `feat(metrics): workspace and module wiring`.

## Task 15: `wh validate` hook + docs

**Files:** Modify `src/wh/cli.py`, `README.md`, `CLAUDE.md`, Test `tests/test_cli.py`

- `validate`: structure always (`load_definitions`); bind checks only when the mirror duckdb file exists; print `N metric models OK (structure only|fully bound)`, warnings listed. Errors → exit 2 (existing WhError path).
- README: metrics section with the design-doc YAML + slice example. CLAUDE.md: current-state bullet → phase 1 delivered; add gotchas learned.
- Final: `uv run pytest` full suite green → commit `feat(metrics): wh validate checks metric models; docs`.

---

## Execution notes

- Strict TDD per task: failing test → verify fail → implement → verify pass → commit. `set -o pipefail` before any `pytest | tail` chaining.
- Never name a module after a verb (`model`, `slice`, `context`, `frame`); `context_ops.py`/`timegrain.py`/`compiler.py` chosen for exactly this reason.
- All error messages: one clear sentence + the fix (WhError convention).
- Structural tests compare `json_serialize_sql` trees, never SQL text; the single formatting canary (design doc) is Task 7's `test_no_join_when_dim_unreferenced`-style coarse guards only.
