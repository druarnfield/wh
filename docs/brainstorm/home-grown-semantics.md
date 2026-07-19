# semantics v2.1 design (DuckDB-native, healthcare-aware)

**Date:** 2026-07-19
**Status:** agreed (brainstorming session)
**Supersedes:** 2026-07-19-semantics-v2-design.md

DuckDB-native semantic layer for reusable metric definitions, authored and
consumed by SQL-literate analysts. Explicitly NOT a self-serve exploration
tool: no guardrails for users who can't write SQL, no access control, no
portability. The scope cut is the design — most semantic-layer complexity
exists to serve requirements this tool rejects.

This revision reworks joins (`join_one`/`join_many`), adds time
intelligence (`compare=`, fiscal calendar, time-aggregation rules), and
adds the healthcare-reporting requirements: ratio measures, small-cell
suppression, incomplete-period handling.

## Design principle

**The generated SQL is the product; correctness is structural.** Every
query is printable and paste-able into the DuckDB CLI. Where a class of
wrong number can be made inexpressible (fan-out, averaged percentages,
summed snapshots, calendar-YTD in a fiscal world), the schema prevents it
rather than documentation warning about it.

## Decisions made

- Expressions are SQL fragments; identifiers quoted; DuckDB `EXPLAIN`
  validates at load. Zero dependencies beyond core wh.
- `join_one` (many-to-one lookup, joined at raw grain in the model's FROM)
  vs `join_many` (fan-out risk, aggregate-then-join between CTEs).
  Cardinality is declared; treatment follows mechanically.
- Namespaced measures with auto-join across models; `metric()` returns a
  DuckDB relation with `.frame()` sugar.
- Time intelligence is query-time (`compare=`), never YAML measure
  variants. Prior-period via self-join on shifted period, never `lag()`
  (gaps make lag lie).
- Every measure carries a `time_agg` rule; stocks and flows aggregate
  over time correctly by construction.
- Fiscal calendar is config; `fy` grains and `fytd` comparisons are
  first-class.
- Ratio measures are a schema type compiled at query grain.
- Suppression and incomplete-period handling are relation-level
  operations, cheap and explicit.

## Config: `wh.yaml`

```yaml
semantics:
  dir: ./semantics
  fiscal_year_start: 7        # July (AU health). Default 1 = calendar.
```

## YAML schema

```yaml
waitlist:
  table: main.waitlist
  description: "Outpatient waitlist snapshots"
  time_dimension: snapshot_date
  snapshot: true                    # stock model: measures default time_agg: last
  dimensions:
    specialty: specialty
    clinic:
      expr: upper(clinic_code)
      description: "Clinic code, normalised"
    region: clinics.region          # dimension from a join_one lookup
  measures:
    patients_waiting:
      expr: count(DISTINCT ur)
      description: "Distinct patients on the list at snapshot"
      # time_agg: last  (inherited from snapshot: true)
    median_wait:
      expr: median(wait_days)
      time_agg: none                # cannot roll up over time at all;
                                    # compare=/coarser grains error clearly
    pct_over_target:
      ratio:                        # ratio measure type
        num: count(*) FILTER (WHERE wait_days > target_days)
        den: count(*)
      description: "% waiting beyond clinically recommended time"
  join_one:
    clinics:                        # lookup, joined in FROM at raw grain
      table: main.clinic_dim
      on: clinic_code = clinics.clinic_code
  join_many:
    activity:                       # cross-model, aggregate-then-join
      on: [specialty, clinic]       # shared dimension NAMES (post-agg)

activity:
  table: main.activity_events
  time_dimension: contact_date      # flow model: measures default time_agg: sum
  dimensions:
    specialty: specialty
    clinic: clinic_code
  measures:
    contacts: count(*)
    distinct_patients:
      expr: count(DISTINCT ur)
      time_agg: none
```

Schema rules:
- **`join_one`** declares a many-to-one lookup: LEFT JOIN in the model's
  base FROM, aliased by key; dimensions may reference `alias.column`
  freely. Fan-out-free by declaration — the author asserts cardinality,
  the tool trusts SQL-literate authors. (A `wh validate --deep` check can
  later verify key uniqueness against the mirror.)
- **`join_many`** declares conformity between models: keys are dimension
  names, joined FULL OUTER between per-model CTEs aggregated to the
  requested dims. Symmetric; declared on either side; conflicting double
  declarations error at load.
- **`time_agg`** per measure: `sum` | `last` | `avg` | `none`. Defaults:
  `sum`, or `last` when the model sets `snapshot: true`. Governs how a
  measure re-aggregates to coarser time grains and whether `compare=`
  windows are valid. `none` = computable only at native grain from base
  rows; requesting it at a coarser grain or with `compare=` errors with
  the reason.
- **`ratio:`** measures compile as `num_agg / NULLIF(den_agg, 0)` at the
  query grain. Averaging pre-computed percentages is inexpressible.
  Ratios re-aggregate over time by re-computing num/den (not averaging),
  and suppress when their den is suppressed.

## Query surface

```python
r = wh.metric("waitlist",
    measures=["patients_waiting", "pct_over_target"],
    dims=["specialty", "region"],
    filters=["snapshot_date >= '2025-07-01'"],
    grain="month",                       # day|week|month|quarter|year|fy|fy_quarter
    compare=["fytd", "yoy"],             # ytd|fytd|yoy|pp (prior period)
    complete_periods=True,               # drop trailing incomplete period
)
r.frame()
r.suppress(n=5)                          # small-cell suppression → relation
r.view("waitlist_summary")               # register as view → marimo SQL cells
wh.metric_sql(...)                       # exact generated SQL, formatted

# cross-model (namespaced, auto-join via join_many)
wh.metric(
    measures=["waitlist.patients_waiting", "activity.contacts"],
    dims=["specialty"], grain="fy_quarter",
    filters={"waitlist": ["removal_reason IS NULL"]},
)
```

Semantics:
- **Grains**: `fy`/`fy_quarter` computed from `fiscal_year_start` (a
  fiscal-date SQL helper macro emitted into the query, e.g. period =
  fy start-anchored truncation). Output column `period`; `fy` also emits
  a `fy_label` column ("FY2025-26") because every report wants it.
- **`compare=`** emits extra columns per valid measure:
  `<m>_ytd`/`<m>_fytd` (expanding window within [fiscal] year,
  partitioned by dims), `<m>_py` + `<m>_yoy_pct` (prior year via
  self-join on `period - INTERVAL 1 YEAR`), `<m>_pp` (prior period,
  self-join on shifted period). Validity by `time_agg`: `sum` → all;
  `last`/`avg` → point-in-time comparisons (`yoy`, `pp`) but no
  cumulative (`ytd` of a stock errors: meaningless); `none` → errors.
  Errors name the measure, the rule, and the fix.
- **`complete_periods`**: uses `_mirror.meta` max-date per underlying
  table to drop (default) or flag (`complete_periods="flag"` → boolean
  `period_complete` column) the trailing partial period. Cross-model:
  the minimum of the models' max dates governs.
- **`.suppress(n=5)`**: nulls count-like measures `< n` and any ratio
  whose den is suppressed; adds nothing else in v1 (complementary
  suppression documented as a later, opt-in pass). Returns a relation —
  composable, inspectable, and visible in code review that suppression
  was applied.
- Cross-model dims must be `join_many` keys between every pair of queried
  models (direct declarations only, no transitive paths). `join_one`
  dims are per-model and usable freely in single-model queries; in
  cross-model queries they're valid only if declared as join keys.

## Compilation

Single model (schematically):

```sql
WITH base AS (
  SELECT <fiscal-aware period expr> AS period,
         <dim exprs> AS <names>,
         <measure exprs / ratio num+den parts>
  FROM <table>
  LEFT JOIN <join_one lookups>
  WHERE <filters>
  GROUP BY ALL
)
SELECT ..., <compare windows / self-joins>, <ratio: num/NULLIF(den,0)>
FROM base ...
```

- `time_agg: last` at coarser-than-native grain compiles as
  last-snapshot-per-period selection (max date within period per dims)
  before aggregation — not `sum`, structurally.
- Ratio internals carry `__num`/`__den` columns through CTEs; the public
  ratio column is computed at the outermost select so `compare=` and
  cross-model joins operate on components, never on percentages.
- Cross-model: per-model CTE (each with its own join_ones, filters,
  time_agg handling) → FULL OUTER on COALESCE'd join dims + period.
- Formatting is stable and indented; golden-SQL tests assert exact text.

## Notebook integration (marimo)

The layer is designed to sit well in marimo's reactive model; BSL did not,
and the reasons are worth encoding rather than remembering.

- **`metric()` is a pure function of plain args.** Strings/lists/dicts in,
  fresh relation out, no state carried between calls. This is what makes
  reactive dashboards work by construction: `mo.ui.dropdown` /
  `mo.ui.date` values wire straight into `metric()` kwargs and cells
  re-run correctly. No stored expression graphs to go stale.
- **YAML edits auto-reload (mtime check).** Marimo's dependency graph
  tracks Python variables, not files — an edited model file plus a cached
  `models()` reads as "the tool is broken." Therefore: `models()` records
  per-file mtimes at load; `metric()`/`models()` do a cheap mtime scan per
  call and reload when definitions changed. Sub-millisecond, no
  `reload=True` discipline required. (`reload=True` stays as the
  belt-and-braces override.)
- **`.view(name)`** on the metric relation registers it as a DuckDB view
  on the session connection, making computed metrics queryable from
  marimo SQL cells (the connection is auto-discovered as an engine). This
  is the bridge between Python-defined metrics and ad-hoc SQL over them
  in one notebook.
- **Laziness note.** A displayed relation re-executes on each re-render;
  fine at mirror scale. `.frame()` materialises (and gets marimo's rich
  table); frames are picklable and therefore `mo.cache`-compatible —
  relations are not. Document the pairing: relation while composing,
  `.frame()` at the edge.
- `metric_sql()` printed in a cell doubles as the report documenting its
  own queries.

## Errors

`SemanticsError` at load (structure, duplicate models, join conflicts,
EXPLAIN failures with DuckDB's message). Query-time misuse (invalid
compare for time_agg, non-conformable dims, unknown names) errors before
execution, naming the definition and the rule. DuckDB runtime errors pass
through with one line naming the model and pointing at `metric_sql()`.

## Testing

- **Golden SQL** (core suite, no DB): args → exact emitted SQL, covering
  grains, fiscal boundary dates, compare columns, ratio compilation,
  join_one FROM assembly, cross-model CTEs.
- **DuckDB-real fixtures**: correctness with intent —
  - fan-out-shaped fixture: join_many cannot double count;
  - gap-month fixture: yoy via self-join stays correct where lag would lie;
  - stock fixture: monthly→quarterly waitlist uses last-snapshot, not sum;
  - ratio fixture: unequal group sizes prove grain-level computation;
  - suppression: counts < n and dependent ratios null;
  - complete_periods against a seeded `_mirror.meta`;
  - fiscal: July–June boundaries for fy, fy_quarter, fytd.
- `wh validate`: EXPLAIN per model; join declaration checks.

## Rollout

1. Compiler core: single-model, grains (incl. fiscal), time_agg, ratio.
2. `compare=` + `complete_periods`.
3. join_one / join_many + namespaced cross-model.
4. `.suppress()` + `metric_sql()` polish + golden-SQL suite hardening.
5. Later: `validate --deep` (join_one key uniqueness), complementary
   suppression, transitive join paths if ever wanted.

## Out of scope

Self-serve safety, access control, non-DuckDB backends, serving/MCP,
measure-level filters in YAML, join-type configuration, transitive join
resolution.
