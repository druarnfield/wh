# metrics design (greenfield: fact, lanes, provenance)

**Date:** 2026-07-19 (reviewed and pinned 2026-07-20)
**Status:** agreed
**Supersedes:** 2026-07-19-semantics-design.md (BSL integration — removed in rollout step 0)

Greenfield design from three fundamentals, deliberately not modelled on
existing metrics systems:

1. A **measure** is a versioned, auditable definition over a fact table.
2. **Slicing** (dice / drill / filter context) must be free and flexible.
3. Slicing must be **structurally unable** to alter a measure's meaning.

Target authors and users: a reporting team fluent in SQL / dbt / data
modelling. The data shape is assumed, not abstracted over: one
denormalised fact table per subject, dimensions hanging off it at 1:many,
patient / doctor / facility-hierarchy style. No cross-fact composition in
the layer (combining two slices is notebook-space: both are relations at
a conformed grain).

## Principles

- **Two predicate lanes, never mixed.** A measure's intrinsic `where` is
  part of its identity, lives in git, and compiles as
  `agg(expr) FILTER (WHERE intrinsic)`. The filter context is extrinsic,
  selects fact rows in scope, and compiles into the outer `WHERE`. The
  lanes are different clauses in the generated SQL; the integrity
  guarantee is the compilation shape, not a policy.
- **Compute from base, always.** Every query aggregates fact rows at the
  requested grain. No reaggregation of aggregates, so drill up/down
  cannot corrupt a number by construction. (`time_agg` survives as the
  one annotation about reality: stocks are not flows.)
- **Declared surface.** Everything sliceable — attributes, hierarchies,
  time — is declared in the model. The context can only reference
  declared attributes with structured operations. Not user-babying:
  provenance-enabling. Arbitrary SQL in the extrinsic lane would make
  audit a text blob instead of data.
- **Numbers explain themselves.** Definitions are content-hashed;
  contexts are data; refresh stamps live in `_mirror.meta`. Every result
  can therefore carry complete provenance: what was computed, under what
  definition version, filtered how, on data from when.

## Config: `wh.yaml`

```yaml
semantics:
  dir: ./semantics
  fiscal_year_start: 7        # July (AU health). Default 1 = calendar.
```

`fiscal_year_start` changes what `fy`, `fy_quarter`, and `fytd` *mean*,
so it is folded into every model hash — same treatment as `snapshot`.

## YAML schema

`semantics/*.yml`, merged as today. Two top-level kinds: **shared
dimensions** (conformed, defined once) and **models** (one per fact
subject).

```yaml
dimensions:                         # shared / conformed — define once
  facility:
    table: main.clinic_dim
    key_column: clinic_code         # the dim-side key
    attributes:
      clinic: clinic_name
      hospital: hospital_name
      district: district
      region: region
    hierarchy: [clinic, hospital, district, region]
  doctor:
    table: main.doctor_dim
    key_column: doctor_id
    attributes:
      specialty: specialty
      seniority: seniority_band

waitlist:
  fact: main.waitlist
  description: "Outpatient waitlist snapshots"
  time:
    column: snapshot_date
    cadence: weekly                 # optional: expected snapshot rhythm
  snapshot: true                    # stock: last-snapshot row selection

  dimensions:
    facility: clinic_code           # shared dim: fact-side key column only
    doctor: doctor_id
    urgency: urgency_category       # local degenerate dim: fact column

  measures:
    patients_waiting:
      expr: count(DISTINCT ur)
      additive: false               # provenance flag: don't sum these rows
      description: "Distinct patients on the list at snapshot"
    long_waiters:
      expr: count(*)
      where: wait_days > 365                    # intrinsic: part of identity
      description: "Patients waiting beyond 365 days at snapshot"
    pct_over_target:
      ratio:
        num: count(*) FILTER (WHERE wait_days > target_days)
        den: count(*)
      description: "% waiting beyond clinically recommended time"
    median_wait:
      expr: median(wait_days)
      time_agg: none
      description: "Median days waiting at snapshot"

removals:
  fact: main.waitlist_removals      # event-grain fact: one row per removal
  description: "Waitlist removal events"
  time:
    column: removal_date
  dimensions:
    facility: clinic_code
    doctor: doctor_id
  measures:
    removals:
      expr: count(*)
      where: removal_reason <> 'ADMIN'          # intrinsic: part of identity
      description: "Clinically meaningful removals"
```

Rules:
- **Shared dimensions are identity.** A model referencing `facility`
  gets *the* facility dimension — same table, attributes, hierarchy —
  supplying only its fact-side key column. "Same name = same dimension"
  is guaranteed by construction; homonyms across models are impossible
  for shared dims. Contexts apply by shared-dim identity, not name
  coincidence. Local (degenerate) dims remain per-model for genuinely
  private attributes; a local dim shadowing a shared dim's name is a
  load error.
- `description` is **required** on every measure (load error without).
  An auditable definition without prose isn't one.
- `where` on a measure is intrinsic: immutable at query time,
  hash-relevant, rendered in provenance. First-class and encouraged —
  this is where "what counts as a removal" lives, in a diff.
  **Intrinsic predicates may reference fact columns only** (load error
  otherwise, enforced via the parse tree against the fact's schema
  fingerprint): a definition whose identity depended on type-1
  dimension attributes would mutate under a stable hash as the dim
  table changes. Identity lives in git; dimension state does not.
- `additive: false` marks measures whose result rows must not be
  re-summed (distinct counts, medians, ratios are implicitly
  non-additive). Carried into provenance; see Provenance.
- **Stocks and flows live in different facts.** `snapshot: true` means
  last-snapshot row selection at coarse grains applies to *every*
  measure of the model — one coherent snapshot per period, always.
  Consequently `time_agg: sum` on a snapshot model is a **load error**:
  counting flow events (removals, additions) from the surviving rows of
  a final snapshot undercounts anything that dropped off mid-period.
  The error names the fix: model the flow as its own event-grain fact
  (see `removals` above). This is data modelling belonging in the
  curated schema, not patched over in the layer; combining a stock and
  a flow in one report is two slices joined in the notebook, which the
  design already blesses.
- **Default `time_agg`:** `last` on `snapshot: true` models, `sum` on
  event-grain facts. Stated per measure only to override (e.g.
  `median_wait`'s `none`).
- `time_agg` otherwise governs **`compare=` validity only**, not
  plain-grain computability: any measure slices at any grain, because
  slices always recompute from base rows — on snapshot models over the
  period's final snapshot, so `median_wait` at `grain="month"` is
  "median at end of month," a well-defined statistic, never a median
  over mixed snapshots. `time_agg: none` restricts `compare=` (no
  cumulative or derived windows), erroring with the rule named.
- **Snapshot selection uses the global period maximum.** "Last snapshot
  in the period" is one *moment* — the max snapshot date present in the
  fact within the period (after time-context truncation) — applied to
  all groups. Per-group maxima would compare clinics at different
  points in time inside one row: mixed-snapshot corruption in disguise.
  Consequence owned plainly: an entity absent from the period's final
  snapshot reads as "not on the list at that moment," which is what the
  data says. A partial final snapshot (feed hiccup, clinic missing) is
  a *data quality* event, detected upstream by a snapshot-completeness
  check in the post-refresh `checks:` mechanism — not silently averaged
  over in the layer. The chosen as-at date is stamped into provenance
  (same disclosure as mid-period truncation; it is the same sentence).
  **The as-at date is computed over the unfiltered fact** — time-context
  truncation applies, attribute context does not. This is
  lane-separation, load-bearing: if `facility__region = North` could
  move the as-at date (North's feed lagging the global final snapshot),
  the extrinsic lane would silently change the *moment* a measure is
  evaluated at — extrinsic input altering measure meaning, which the
  design forbids. Filtered and unfiltered slices of a period therefore
  always share one moment; a lagging group reads as absent on that
  date, which the completeness checks then catch.
- **Dimensions are type-1 (current structure), deliberately.** Shared
  dims reflect the mirror's current state: when a clinic is reassigned
  to a new district, every historical slice reports it under today's
  structure — which is what health performance reporting usually means
  by "how is North doing." This is a semantic decision, not an
  accident, and the number discloses it: provenance renders "dimension
  attributes as-at <dim table refresh timestamp>" from `_mirror.meta`.
  Point-in-time structure, if ever needed, is an SCD2 dimension
  modelled in the curated schema and declared like any other — the
  layer needs no history machinery.
- **`strict_context: true` at model level** makes ignored context
  entries errors for every slice of that model (per-slice
  `strict_context=` still overrides). Governance you can forget isn't
  governance; a governed model declares its own strictness the way it
  declares everything else.
- Time is a built-in dimension: calendar + fiscal hierarchy derived from
  `time.column`, no user YAML for it.
- **Load-time integrity checks** (phase 1, not deferred), cached per
  (mirror refresh stamp, table), surfaced through `wh validate` and any
  future post-refresh `checks:` mechanism:
  - every shared dim table: `count(*) = count(DISTINCT key_column)` —
    duplicate keys silently inflate every non-distinct measure via join
    fan-out; **load error**.
  - every fact→dim key: orphan count (fact keys missing from the dim) —
    orphans survive the LEFT JOIN into unfiltered totals but vanish
    under attribute filters and group into a NULL row under `by=`, so
    region totals ≠ sum of regions. A **warning**, not an error (a new
    clinic ahead of the dim refresh is a fact of life in health data),
    and stamped into `provenance.data` ("facility: 34 orphan keys,
    0.1%") — the number discloses its own reconciliation gap.

## Filter context

Immutable value object; structured operations over declared attributes
only:

```python
ctx = wh.context(
    facility__region="North",                 # equality
    doctor__specialty=["ENT", "Ophthal"],     # membership
    time=("2025-07-01", "2026-06-30"),        # range on the time column
    urgency=wh.not_("Cat 3"),                 # small op vocabulary
)
ctx2 = ctx.with_(facility__district="Coastal")
ctx3 = ctx.without("doctor__specialty")
merged = ctx | overrides                      # right side wins per attribute
```

- Ops vocabulary (v1): equality, `in`, `not_`, range/between (date
  ranges **inclusive on both ends** — `BETWEEN` semantics, matching the
  FY example above), relative-time helpers (`wh.last(12, "month")`).
  Nothing else; no SQL.
- Attribute addressing `dimension__attribute` (double-underscore) at the
  Python surface, `dimension.attribute` in `by=`.
- **Miss rule:** context entries apply by **shared-dim identity** — an
  entry on `facility__region` applies to every queried model that
  references the shared `facility` dimension, and is *skipped and
  recorded* for models that don't (provenance lists `applied` and
  `ignored` separately). Homonym collisions are impossible for shared
  dims by construction; local-dim entries apply only to the declaring
  model. `slice(..., strict_context=True)` turns ignored entries into
  errors for governed outputs.
- **Empty selections:** an empty value from a *widget* means "no
  selection" → unfiltered, recorded in provenance as
  `empty selection → unfiltered` (slicer semantics). A *literal* empty
  list is an error, always — programmatically produced emptiness almost
  always means upstream logic failed, and silently meaning "everything"
  is the footgun. The seam is real: `wh.context(facility__region=
  widget.value)` is natural to write and collapses the distinction, so
  passing `.value` is the documented anti-pattern and the error
  teaches the fix: *"if this came from an empty widget, pass the widget
  itself (its value is read at slice time) or use `wh.all()` to mean
  unfiltered."* The friction is deliberate; accepting `.value` would
  simply delete the safety check.
- **Resolution.** A context holding widgets or relative-time helpers is
  *unresolved* — not yet plain data. `slice()` resolves it to a
  plain-data snapshot: widget `.value`s are read, and `wh.last(12,
  "month")` is anchored to the fact's **max time value from
  `_mirror.meta`** (not wall clock — the same context on the same mirror
  always selects the same rows, and stale data doesn't silently shrink
  the window). Hashing, serialisation, and provenance are defined over
  the **resolved** context only; provenance records the resolved
  absolute window alongside the helper that produced it
  ("last 12 months → 2025-07-15..2026-07-14").
- Contexts are plain data *once resolved*: serialisable, hashable,
  diffable — they appear in provenance verbatim.

## Query surface

```python
m = wh.model("waitlist")                      # model handle

r = m.slice(
    measures=["patients_waiting", "pct_over_target"],
    by=["facility.region", "doctor.specialty"],
    context=ctx,
    grain="month",                # one time-hierarchy level; output carries one
                                  # period column (period-start date)
    compare=["yoy"],              # fytd would error here: stock measure (see time_agg)
    complete_periods=True,
)
r.frame()          # preferred-backend frame
r.view("summary")  # register as view for marimo SQL cells
r.suppress(n=5)    # small-cell suppression (ratios with suppressed den too)
r.sql              # exact generated SQL, formatted, stable
r.provenance()     # see below

wh.slice("waitlist", ...)                     # module-level sugar
```

- Drill down = re-slice with a deeper hierarchy level in `by=`. Correct
  automatically (compute-from-base); no drill state in the layer.
- `compare=` validity derives from `time_agg` (sum → all; last/avg →
  point-in-time only, no cumulative; none → error naming the rule).
  Prior-period via self-join on shifted period, never lag.
- **`compare=` widens the time scan.** The context's time entry defines
  the *output* window; comparison columns may read base rows outside it
  (context ∪ shifted context — yoy under an FY context scans the prior
  FY too). Non-time context entries apply to comparison rows unchanged
  (North compares against North-prior-year). Provenance records both
  the widening ("yoy: scan widened to 2024-07-01") and its **coverage**
  against `_mirror.meta`'s min/max for the fact's time column ("fact
  data begins 2024-09-01 — prior year partially uncovered") — so a
  NULL comparison from genuinely missing data is distinguishable from
  a bug at zero query cost. Silent NULLs from an unwidened scan would
  be exactly the failure class this design exists against; erroring
  instead would outlaw the most common governed query (FY context +
  yoy).
- Time is the one hierarchy addressed via `grain=`/`compare=` rather
  than `by=` because comparison semantics — shifting, widening,
  cumulative windows — hang off it and off nothing else; the API
  asymmetry mirrors a real asymmetry in the domain.
- `complete_periods` from `_mirror.meta` max dates as before — with a
  declared meaning per fact kind. Flow facts: mirror max date decides.
  Snapshot facts: "complete" means the period's *final expected*
  snapshot landed, which max date alone can't tell (weekly cadence, last
  snapshot on the 28th: complete or truncated?). With `time.cadence`
  declared, completeness is computed against the expected rhythm;
  without it, the max-date heuristic applies and the doc (and
  provenance) call it out as weaker on snapshot models.
- Per-call `filters=` is gone. The context *is* the extrinsic lane;
  one mechanism, fully recorded. Ad-hoc exploration beyond declared
  surface belongs in SQL over the mirror — the layer is for governed
  numbers.

## Provenance

```python
p = r.provenance()
p.measures      # [(name, definition text incl. intrinsic where, hash)]
p.context       # applied entries, ignored entries
p.shape         # by, grain, compare, complete_periods, suppression
p.data          # per-table refresh timestamps from _mirror.meta
p.sql           # the generated query
p.render()      # text block for report footers / notebook display
p.to_dict()     # stampable into outputs (Excel footer, push metadata)
```

Measure hash = content hash of a **minimal semantic projection wh owns**
of the parse tree — a stable subset (operators, identifiers, literals,
structure) extracted from `json_serialize_sql` output rather than the
raw serialization, because DuckDB's tree format is not a stability
contract and an engine upgrade must not bump every definition hash with
zero semantic change. The running DuckDB version is stamped into
`provenance.data` regardless, so if a hash shift ever does trace to an
engine change, it is explainable. Hash inputs: expr + intrinsic where +
time_agg + ratio parts + the fact reference. A **model hash** over the
normalised model config —
`snapshot` flag, time column and cadence, dimension key mappings,
fiscal_year_start — is stamped
alongside, because those determine results just as surely (flipping
`snapshot: true` changes what `median_wait` *means* while its measure
hash is stable). Definition version = (measure hash, model hash) pair.
`provenance.data` additionally carries a **schema fingerprint** of each
referenced table (columns + types from the mirror), so structural
upstream drift is visible even when definitions are stable.

Further disclosures carried by provenance: **each comparison period's
own as-at date** on snapshot models ("FY25 as-at Jun 30 vs FY24 as-at
Jun 28" is exactly what the sentence should say), and a
**strictness-weakened flag** whenever a per-slice
`strict_context=False` overrides a model's declared
`strict_context: true` — the escape hatch stays, but it confesses.

**Contract boundary, stated plainly:** the hash covers the layer's
definition; it cannot see upstream *logic* changes inside
`main.waitlist`'s curated view. That is precisely why the curated schema
lives in git — its commit hash is the missing link, and a later hook can
stamp it into `provenance.data`. An honest boundary beats false
coverage.

Non-additive measures (`additive: false`, plus implicitly distinct
counts, medians, ratios) are flagged in `p.shape` and named in
`p.render()` — extending "numbers explain themselves" past the layer
boundary, since nothing stops someone summing `patients_waiting` rows in
a returned frame. The warning travels with the number; frame-level
metadata does not survive backend round-trips and is not attempted.

The rendered form is the sentence a number should be able to say for
itself — including when the context quietly redefines a period: a time
range ending mid-month makes "end of month" mean "as at July 14th," and
the sentence says so ("month of July, as at 2026-07-14 — period
truncated by context"; `complete_periods` is the adjacent tool when
truncation isn't wanted):

> patients_waiting [a3f2c1 · model 9d41e0] = count(DISTINCT ur) at
> snapshot · context: facility.region = North; time in FY2025-26 ·
> by facility.region, month (snapshot as-at last date in period) ·
> yoy scan widened to 2024-07-01, fully covered ·
> dimension attributes as-at 2026-07-19 02:00 ·
> data as at 2026-07-19 02:00

## Compilation

```sql
SELECT <time level expr> AS period,
       <by attribute exprs>,
       count(DISTINCT ur)                        AS patients_waiting,
       count(*) FILTER (WHERE wait_days > 365)   AS long_waiters,
       <ratio: __num, __den carried; division outermost>
FROM main.waitlist
LEFT JOIN main.clinic_dim AS facility ON clinic_code = facility.clinic_code
LEFT JOIN main.doctor_dim AS doctor   ON doctor_id = doctor.doctor_id
WHERE <context lane: structured ops compiled with correct quoting>
GROUP BY ALL
```

- Intrinsic lane: `FILTER (WHERE ...)` per measure. Extrinsic lane:
  outer `WHERE`. Visibly separate in every emitted query.
- On `snapshot: true` models, coarse grains compile last-snapshot-per-
  period row selection — the **global** max snapshot date within the
  period (after time-context truncation), applied to all groups —
  before any aggregation, so every statistic is computed over one
  coherent snapshot, never mixed rows. **The as-at subquery contains
  time-context predicates only — never attribute predicates** (see the
  lane rule in the schema section). The as-at date lands in provenance.
- The emitted dialect is **DuckDB, as a stated commitment** — `GROUP BY
  ALL`, `FILTER`, `json_serialize_sql` are relied on deliberately. "The
  compilation shape is the guarantee" is a claim about one dialect;
  warehouse pushdown would be a new design, not a port.
- Empty `in` selection = no filter (slicer semantics). NULLs excluded
  from generated membership tests unless `wh.null` op used explicitly.
- Ratio measures compile as an inner aggregate select carrying
  `__num`/`__den`, wrapped by an outer select performing the division —
  the wrapper level exists only when a ratio measure is present (the
  sketch above shows the ratio-free single-level shape).
- Only joins for dimensions actually referenced (by, context, measure
  exprs) are emitted — keeps `r.sql` minimal and readable.
- Stable formatting because `r.sql` is a product feature (readable,
  paste-able) — but tests assert parse-tree content, not text (see
  Testing).

## Notebook integration (marimo)

Carried from v2.1, adjusted to the context design:

- `slice()` is a pure function of visible args (model, measures, by,
  context, grain). Context is a value flowing through marimo's graph —
  cells depending on `ctx` re-run exactly when it changes. No ambient
  state anywhere.
- YAML mtime auto-reload on `model()`/`slice()` calls; `reload=True`
  override survives.
- **Widgets from declared surface:** `m.filter_dim("facility.region")` →
  populated `mo.ui` element (options = DISTINCT over the *dimension
  table* — cheap, no fact scan); `m.filter_date()` → daterange with real
  bounds. Widgets and raw values both accepted where context values go:
  `wh.context(facility__region=widget)` reads `.value` at slice time.
- Associative behaviour is user-space composition:
  `m.filter_dim("facility.clinic", context=ctx.without("facility__clinic"))`
  re-derives possible values — Qlik's exclude-your-own-field rule as a
  visible argument. Cascading = the same call downstream of another
  widget; marimo's graph handles ordering. No engine, no magic.
- Relations lazy / `.frame()` materialises / frames are `mo.cache`-able;
  document the pairing.

## Errors

Load: `SemanticsError` — missing descriptions, bad keys, EXPLAIN
failures (DuckDB validates every model's full SELECT at load), hierarchy
attrs not declared. Slice-time misuse (unknown attribute, invalid
compare for time_agg, strict-context violation) errors before execution
naming the rule. DuckDB runtime errors pass through, one line pointing
at `r.sql`.

## Testing

Content over formatting: tests assert what queries *mean*, never how
they're pretty-printed. DuckDB's `json_serialize_sql()` provides the
parse tree; the formatter is deliberately untested except for one canary.

- **Structural equality:** emitted SQL and expected SQL parsed via
  `json_serialize_sql`, trees normalised (whitespace-free already;
  generated aliases canonicalised) and compared. Formatting changes are
  invisible; semantic drift fails.
- **Lane & pruning invariants (tree-walk properties):** the design's
  promises asserted directly on the parse tree, immune to any refactor
  that preserves them. The lane invariant is directional, because
  authored `FILTER` clauses legitimately appear inside measure exprs
  (ratio numerators): *no context-derived predicate appears anywhere
  except the outer `WHERE`, and every outer-`WHERE` predicate derives
  from the context.* Implemented by compiling with **canary literals**
  substituted into context values and asserting the canaries appear
  only in the outer `WHERE` of the tree — distinguishing
  compiled-from-context from authored SQL without fragile position
  rules. On snapshot models the canary rule extends to the as-at
  subquery: **time canaries may appear there; attribute canaries must
  never** — the lane rule for the evaluation moment, tested the same
  way. Plus:
  - no dimension join emitted unless referenced by `by`, context, or a
    measure expression;
  - ratio division appears only in the outermost select, components
    carried as `__num`/`__den` beneath.
- **Lane-isolation property test:** for a grid of contexts, intrinsic
  measure results over the *same in-scope rows* computed two ways (layer
  vs hand-written) agree; no context can change a measure's predicate.
- **Behavioural fixtures with intent** (results, not text): gap-month
  (yoy self-join vs lag), stock rollup (last-snapshot not sum, incl.
  median-at-end-of-month vs median-of-mixed-rows; global-max fixture
  where one clinic's final snapshot lags — absent from the period row,
  as-at date stamped; filtered-vs-unfiltered slices of the same period
  share one as-at even when the filtered group's feed lags),
  flow-on-snapshot
  declaration fails at load naming the event-fact fix, compare-widening
  (FY context + yoy yields prior-year values, not NULLs; widening
  recorded; retention-edge variant where the shifted window falls off
  the data and provenance reports partial coverage), ratio at unequal group sizes, suppression incl. dependent
  ratios, complete_periods vs seeded meta, ignored-context recording,
  duplicate-dim-key fixture (load fails, naming the table),
  orphan-key fixture (warning + provenance stamp; drill consistency
  asserted *with* the NULL group present, region totals reconciling only
  when it's included), literal-empty-selection error vs widget-empty
  pass-through, hierarchy drill consistency (region totals = sum of
  district slices for additive measures), intrinsic-where referencing a
  dimension attribute fails at load naming the rule, cadence-declared
  completeness (weekly snapshots: month with final expected snapshot =
  complete; without it = incomplete even though max date looks
  plausible).
- **Provenance snapshot tests:** rendered block stable; hash changes
  iff definition changes; **hash stability across serializer noise** —
  the semantic projection yields identical hashes for reformatted but
  equivalent definitions, and the DuckDB version stamp is present so
  engine-upgrade shifts are explainable.
- **One formatting canary:** a single representative `r.sql` snapshot,
  marked "regenerate freely" — deliberate notification when the emitted
  shape changes, without veto power over the formatter.

## Rollout

0. Remove the BSL integration: `semantics.py`, `test_semantics.py`, the
   `[semantics]` extra, the CLI validate hook, and the CLAUDE.md BSL
   notes — keeping `docs/upstream/` (the issue drafts stand on their
   own). Frees the `wh.model` name for the new surface. The new
   implementation module is `metrics.py` — NOT `model.py`/`slice.py`
   (phase-1 submodule/verb shadowing gotcha).
1. Model loading + validation (incl. shared dims + dim-key uniqueness
   checks) + context object + single-fact `slice()` with lanes, grains
   (incl. fiscal), snapshot row-selection, `time_agg`, ratios.
   Structural + invariant test suite.
2. `compare=`, `complete_periods`, `.suppress()`.
3. Provenance object + render + tree-based measure hashing + schema
   fingerprints.
4. marimo widgets + possible-values helper.
5. Later: curated-schema git hash stamped into provenance,
   `wh.compose()` for cross-fact derived numbers (removals ÷
   patients_waiting) carrying concatenated provenance from both slices
   plus the composition expression — composed numbers currently fall
   off the provenance cliff, and this request will arrive early,
   complementary suppression, context serialisation to/from YAML for
   saved report configs.

## Performance envelope

Compute-from-base means every slice and drill rescans the fact table.
The assumed ceiling, written down so it is revisited consciously:
fact tables to low hundreds of millions of rows, interactive slices
under ~1s on workstation DuckDB. The current mirror (~10GB total) sits
comfortably inside it. Aggregate awareness (pre-computed rollups the
layer would route to) is foreclosed **by principle**, not oversight:
pre-aggregation reintroduces the reaggregation correctness problems —
stocks, distincts, medians, ratios — that compute-from-base exists to
kill. If the ceiling is ever hit, the tradeoff is reopened as a design
decision with this section as its starting point, not patched with a
cache that silently breaks the correctness story.

## Out of scope

Cross-fact joins in the layer, raw-SQL filter contexts, drill state,
access control, self-serve safety, non-DuckDB backends, serving/MCP.
