# The metrics layer — user guide

Governed numbers over your DuckDB mirror. You declare **measures** (versioned,
auditable definitions over a fact table) in YAML; you slice them freely in
Python; the layer guarantees — structurally, in the generated SQL — that no
amount of slicing can change what a measure *means*. Every result can explain
itself: definition, version hash, filters, evaluation moment, data freshness.

Three rules drive everything else:

1. A measure's own predicate (`where:`) compiles into a per-measure
   `FILTER (WHERE ...)` clause. Your filter context compiles into the outer
   `WHERE`. They are different clauses in every emitted query — the lanes
   never mix.
2. Every query aggregates base fact rows at the requested grain. Nothing is
   ever re-aggregated from aggregates, so drilling up or down cannot corrupt
   a number.
3. Everything sliceable is declared. The context can only reference declared
   attributes with a small vocabulary of operations — which is what makes
   full provenance possible.

## Quick start

`semantics/*.yml` next to `wh.yaml` (directory configurable via
`semantics: dir:`):

```yaml
waitlist:
  fact: main.outpatient_waitlist_snapshot
  description: "Monthly outpatient waitlist censuses"
  time:
    column: CensusDate
    cadence: monthly
  snapshot: true
  dimensions:
    specialty: Specialty          # local dim: a fact column
  measures:
    patients_waiting:
      expr: count(DISTINCT PatUrnCoded)
      description: "Distinct patients on the list at census"
    long_waiters:
      expr: count(*)
      where: WaitingTime > 365    # intrinsic — part of the definition
      description: "Entries waiting beyond 365 days at census"
```

```python
import wh

s = wh.slice("waitlist",
             measures=["patients_waiting", "long_waiters"],
             by=["specialty"], grain="month", compare=["prior"],
             context=wh.context(time=wh.last(6, "month")))
s.frame()                # your preferred dataframe backend
s.sql                    # the exact generated SQL
s.provenance().render()  # the audit block
```

`uv run wh validate` checks every model — structure always, full bind checks
(schema, key uniqueness, orphans) when the mirror file exists.

## Defining models

### Top-level YAML

Two kinds of top-level keys, merged across all files in `semantics/`:

| Key | Meaning |
|---|---|
| `dimensions:` | **Shared (conformed) dimensions** — defined once, referenced by name from any model. Duplicate definitions across files are a load error. |
| anything else | A **model**: one fact table and its measures. |

### Shared dimensions

```yaml
dimensions:
  facility:
    table: main.clinic_dim
    key_column: clinic_code
    attributes:                   # attribute name -> dim-table column
      clinic: clinic_name
      region: region
    hierarchy: [clinic, region]   # optional, finest first; entries must be attributes
```

A model references a shared dim by name, supplying only its fact-side key
column (`facility: clinic_code`). Same name = same dimension, everywhere —
that identity is what lets one context apply across models.

### Models

Unknown keys anywhere in a definition are a load error — typos never
silently change a model's semantics.

| Key | Required | Meaning |
|---|---|---|
| `fact:` | yes | The fact table (`schema.table`). |
| `description:` | no | Prose for humans. |
| `time: column:` | yes | The time column (DATE or TIMESTAMP). Time is a built-in dimension — no YAML for calendars. |
| `time: cadence:` | no | `daily` / `weekly` / `monthly` — the expected snapshot rhythm; sharpens `complete_periods` on snapshot models. |
| `snapshot:` | no (false) | `true` = this fact is a **stock** (repeated censuses). See below. |
| `strict_context:` | no (false) | `true` = context entries that don't apply to this model are errors, not silently skipped (per-slice `strict_context=` still overrides — and provenance confesses the override). |
| `dimensions:` | no | `name: fact_column`. If `name` matches a shared dim, it's a reference; otherwise it's a **local (degenerate) dim** — the fact column itself is the attribute. |
| `measures:` | yes (≥1) | See next. |

### Measures

```yaml
measures:
  pct_over_target:
    ratio:
      num: count(*) FILTER (WHERE wait_days > target_days)
      den: count(*)
    description: "% waiting beyond clinically recommended time"
```

| Key | Rules |
|---|---|
| `description:` | **Required.** An auditable definition without prose isn't one. Not part of the version hash — reword freely. |
| `expr:` | A DuckDB **aggregate** expression over **fact columns only**. Exactly one of `expr:` / `ratio:`. A non-aggregate expr, or one referencing dimension attributes, is rejected at bind time (a definition depending on a type-1 dim would silently mutate; put attribute logic in the curated schema). |
| `where:` | The intrinsic predicate — first-class and encouraged; this is where "what counts as a removal" lives, in a diff. Fact columns only. Compiles to `expr FILTER (WHERE ...)`; can't be combined with an expr that already has a FILTER (fold it in). |
| `ratio:` | `num:` + `den:` aggregate exprs; division happens once, in the outermost select (never averaged over groups). No `where:` on a ratio — put predicates in the num/den FILTERs. |
| `time_agg:` | `sum` \| `last` \| `avg` \| `none`. **Defaults: `last` on snapshot models, `sum` on event facts.** Governs which comparisons are valid (see `compare=`), never plain-grain computability. `sum` on a snapshot model is a load error (model the flow as its own event fact). |
| `additive: false` | Marks results that must not be re-summed. Distinct counts, medians, modes and ratios are non-additive automatically; provenance names them. |

### Naming rules

All names and columns are spliced into SQL, so they must be plain
identifiers (letters, digits, underscores). `fact`, `period`, `time` and
`__`-prefixed names are reserved. Violations are load errors.

## Stocks vs flows (`snapshot: true`)

A snapshot fact holds repeated censuses of the same population. The layer
enforces one iron rule: **every statistic is computed over exactly one
coherent census per period** — the *global* last census within the period
(never per-group, never mixed rows). Consequences you should expect:

- `median_wait` at `grain="month"` means "median at end of month".
- An entity absent from a period's final census reads as "not on the list at
  that moment" — even if its feed is lagging. Filtering to the lagging group
  does **not** move the evaluation moment (that would let a filter change a
  measure's meaning); a partial final census is a data-quality event, not
  something the layer averages over.
- Counting flow events (removals, additions) from surviving snapshot rows
  undercounts — hence the `time_agg: sum` load error. Flows get their own
  event-grain fact.

The chosen as-at date(s) appear in provenance.

## Querying

```python
m = wh.model("waitlist")                      # bound model handle
s = m.slice(
    measures=["patients_waiting", "pct_over_target"],
    by=["facility.region", "specialty"],      # shared: dim.attr — local: name
    context=ctx,
    grain="month",
    compare=["prior", "yoy"],
    complete_periods=True,
    strict_context=None,                      # None = model default
)
wh.slice("waitlist", ...)                     # module-level sugar
s.frame(backend=None)   # execute -> polars/pandas/pyarrow
s.view("summary")       # register a DuckDB view for marimo SQL cells
s.sql                   # the exact generated SQL, stable and pasteable
s.suppress(n=5)         # a NEW slice with small-cell suppression
s.provenance()          # see Provenance
m.warnings              # bind-check warnings (orphan keys etc.)
```

Drill down = re-slice with a deeper hierarchy level in `by=`. There is no
drill state; compute-from-base makes it correct automatically.

### Grains

`day`, `week` (Mon-start), `month`, `quarter`, `year`, `fy`, `fy_quarter`.
Fiscal grains come from `wh.yaml`:

```yaml
semantics:
  fiscal_year_start: 7      # July (AU). Default 1 = calendar.
```

`grain=` takes one level and yields one `period` column (period-start date).
Time is addressed via `grain=`/`compare=`, never `by=`.

### The filter context

An immutable value object over declared attributes — no SQL, ever.

```python
ctx = wh.context(
    facility__region="North",                 # equality (dim__attr addressing)
    specialty=["ENT", "Ophthalmology"],       # membership
    category=wh.not_("Cat 3"),                # negation (keeps NULL rows —
                                              #   "not Cat 3" includes unknowns)
    time=("2025-07-01", "2026-06-30"),        # day-inclusive range
    # time=wh.last(12, "month")               # relative — anchored to the
                                              #   mirror's max date, never wall clock
)
ctx2 = ctx.with_(facility__district="Coastal")
ctx3 = ctx.without("specialty")
merged = ctx | overrides                      # right side wins per attribute
```

The complete op vocabulary: equality, list membership, `wh.not_()`,
`(start, end)` time ranges, `wh.last(n, unit)`, `wh.all()` (explicitly
unfiltered). That's all of it, on purpose — arbitrary predicates would turn
provenance into a text blob. Values must be scalars or dates; `None`, nested
lists and NaN are errors (NULL matching isn't supported yet).

**How entries apply (the miss rule):** by shared-dim identity — an entry on
`facility__region` applies to every queried model referencing the shared
`facility` dim, and is *skipped and recorded* on models that don't
(provenance lists applied and ignored separately). `strict_context` turns
skips into errors for governed outputs.

**Widgets and empties:** marimo widgets are accepted anywhere a value goes —
their `.value` is read at slice time, and an **empty widget selection means
unfiltered** (recorded in provenance). A **literal empty list is always an
error**: programmatic emptiness usually means upstream logic failed, and
silently meaning "everything" is the footgun. If you were about to pass
`widget.value`, pass `widget` instead.

### `compare=` — prior / yoy / fytd

Comparison columns hold the comparison **value** (`removals_yoy` = the value
one year earlier; deltas are your arithmetic). Requires `grain=`.

| | `prior` | `yoy` | `fytd` |
|---|---|---|---|
| meaning | previous period | same period, prior year | fiscal-year-to-date cumulative |
| `time_agg: sum` | ✓ | ✓ | ✓ |
| `time_agg: last`/`avg` (stocks) | ✓ | ✓ | ✗ (cumulative on a stock is meaningless) |
| `time_agg: none` | ✗ | ✗ | ✗ |

Mechanics worth knowing:

- Comparisons are **shifted self-joins, never `lag()`** — a gap period stays
  NULL instead of silently picking up the wrong row.
- The context's time entry defines the *output* window; comparisons may read
  rows outside it (yoy under an FY context scans the prior FY too). The
  widening and its coverage against your actual data start are recorded in
  provenance — a NULL from genuinely missing history is distinguishable from
  a bug.
- Non-time context follows the comparison rows: North compares against
  North-prior-year.
- On snapshot models, each comparison period is evaluated at **its own**
  as-at census; both moments appear in provenance.
- `fytd` recomputes from base rows every time, so `count(DISTINCT ...)` fytd
  is a true distinct count, not a sum of monthly distinct counts.
- `yoy` is invalid at `week` grain (a year shift misaligns week starts).

### `complete_periods=True`

Drops trailing periods the data can't fully cover. Event facts: the period
must end on or before the fact's max date. Snapshot facts with a declared
`cadence`: the period's *final expected* census must have landed (a weekly
feed whose last snapshot is the 28th completes a 31-day month — the max-date
rule alone would wrongly drop it). A period truncated by your context is by
definition incomplete and is dropped too. Limitation: `monthly` cadence only
sharpens grains coarser than month.

### `.suppress(n)`

Small-cell suppression for publishable outputs: cells with fewer than `n`
fact rows read NULL, and a ratio is also NULLed when its own denominator is
under `n` (a big cell can hide a tiny denominator). Comparison columns
suppress against their own period's cells. Note: the threshold counts the
cell's raw rows, not each measure's filtered contribution.

## Provenance

```python
p = s.provenance()
p.measures      # (name, definition text, content hash) per measure
p.context       # applied / ignored / "empty selection -> unfiltered"
p.shape         # by, grain, compare, completeness, suppression,
                # non-additive names, strictness-weakened flag
p.data          # schema fingerprints, refresh timestamps (_mirror.meta),
                # scan widening + coverage, as-at moments, truncation,
                # DuckDB version, orphan-key warnings, model hash
p.sql           # the generated query
p.render()      # the report-footer block
p.to_dict()     # JSON-safe — stamp into Excel footers, push metadata
```

Definition version = (measure hash, model hash). The measure hash covers the
expr, intrinsic where, ratio parts, `time_agg` and the fact reference — over
a *semantic projection* of the parse tree, so reformatting, case, and DuckDB
serializer noise cannot shift it. The model hash covers everything else that
determines results (`snapshot`, time column, cadence, dim mappings,
`fiscal_year_start`). Changing a description changes neither. The hash
cannot see logic changes inside the curated views the facts point at — that
boundary is honest, not covered.

## marimo widgets

```python
m = wh.model("waitlist")
region = m.filter_dim("facility.region")   # multiselect; options from the
                                           #   dim table — no fact scan
dates = m.filter_date()                    # date range over real fact bounds
ctx = wh.context(facility__region=region, time=dates)

# cascading is composition, not magic — exclude-your-own-field yourself:
clinic = m.filter_dim("facility.clinic", context=ctx.without("facility__clinic"))
m.values("facility.clinic", context=ctx)   # the raw list, no marimo needed
```

`slice()` is a pure function of its visible arguments, so marimo's dataflow
re-runs exactly the right cells. YAML edits are picked up automatically
(mtime-based) on the next `model()`/`slice()` call.

## What the layer will not do

Deliberate boundaries, not roadmap gaps:

- **No query dialect.** No raw SQL in contexts, no per-call `filters=`, no
  computed measures at slice time. Ad-hoc exploration belongs in SQL over
  the mirror (`wh.connect()`); the layer is for governed numbers.
- **No cross-fact composition.** Combining a stock and a flow (e.g. removals
  ÷ patients_waiting) is two slices joined in your notebook — both are
  relations at a conformed grain. (`wh.compose()` with stitched provenance
  is on the design's later list.)
- **No pre-aggregation, ever.** Every slice rescans the fact table; that is
  the correctness guarantee, and the envelope (fact tables to low hundreds
  of millions of rows, interactive on workstation DuckDB) is written down in
  the design doc. If you hit the ceiling, that's a design conversation, not
  a cache.
- **Dimensions are current-state (type 1).** History reports under today's
  structure; the dim-table refresh timestamp is disclosed in provenance.
  Point-in-time structure = an SCD2 dimension in the curated schema,
  declared like any other.
- **DuckDB only.** The compilation shape *is* the guarantee, and it's a
  claim about one dialect. Warehouse pushdown would be a new design.
- **No NULL matching in contexts yet** (`wh.null` is a future op), no
  access control, no drill state, no serving/API layer.

## Errors

Every error is one sentence plus the fix, and misuse fails **before**
execution: load errors name the file and rule; slice-time errors name the
declared surface; only genuine engine failures pass through raw (pointing
at `s.sql`). If you see an inscrutable DuckDB error, that's a bug — report
it.
