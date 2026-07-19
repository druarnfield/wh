# wh semantics design (Boring Semantic Layer integration)

**Date:** 2026-07-19
**Status:** agreed + adversarially reviewed (refines `docs/brainstorm/semantic-integration.md`;
all package claims verified against boring-semantic-layer 0.3.15 / ibis-framework 12.0)

Add a semantic layer to `wh` so metric definitions live in one place and every
notebook computes the same numbers. Built on
[boring-semantic-layer](https://github.com/boringdata/boring-semantic-layer)
(BSL), which sits on Ibis: models defined once in YAML, queried from
notebooks, compiled to SQL against the local mirror. Notebook-only for v1 —
no serving, no CLI query surface.

## Design principle

**Definitions in YAML, execution through the session connection, sugar on
top.** `wh` owns exactly one piece of query logic: the kwargs→fluent mapping
inside `metric()` (BSL v2's query surface is fluent —
`model.group_by(...).aggregate(...)` — the kwargs form no longer exists in
the library API). Everything else — filter semantics, time grains, joins,
windows — is BSL's, reached by dropping down one layer:

```python
import wh

models = wh.models()                  # all semantic models, bound to the mirror
df = wh.metric("waitlist",            # the 90% one-liner → preferred frame
    measures=["patients_waiting", "median_wait_days"],
    dims=["specialty", "clinic"],
    filters=[_.wait_days > 30],       # optional; ibis deferred / lambdas
    order_by=[("patients_waiting", "desc")],
    limit=100,
    backend="pandas",                 # optional, like pull()
)
wl = models["waitlist"]               # raw BSL semantic table (an Ibis expr):
                                      # fluent API, windows, joins, .sql()
```

## Decisions made

- **Definitions are YAML** in a `semantics/` directory, loaded with BSL's
  `from_yaml(path, tables={...})`. Git-diffable, reviewable by non-Python
  colleagues, and insulated from BSL Python-API churn (v1→v2 reworked the
  Python surface; the YAML schema stayed stable). Python model definitions
  remain possible as an escape hatch, not the packaged path.
- **One DuckDB instance.** Ibis wraps the existing session connection
  (`ibis.duckdb.from_connection(ws.con)`) — never a second connection to the
  file (mixed-mode connections are forbidden in-process; a second handle
  would break swap semantics). Consequence: `register()`ed frames and
  `land()`ed tables bind into models like any mirror table.
- **`metric()` owns the mapping.** Its entire implementation (order matters —
  review-verified):

  ```python
  q = self.models()[model]        # unknown model → SemanticsError listing models
  # validate requested names against the model's declared dims/measures FIRST:
  # BSL's own error lists raw table columns, not semantic names — misleading.
  for f in filters:  q = q.filter(f)      # BEFORE aggregation: filters are
                                          # row-level, may reference any raw
                                          # column (post-agg placement dies at
                                          # execute time on non-measure refs)
  if dims:     q = q.group_by(*dims)
  q = q.aggregate(*measures)
  for o in order_by: q = q.order_by(_translate(o))
  if limit:    q = q.limit(limit)
  return from_arrow(q.to_pyarrow(), self._backend(backend))
  ```

  `order_by` accepts `"col"` (ascending) or `("col", "desc"|"asc")` — BSL
  does NOT take tuples, so `_translate` maps tuples to `ibis.desc/asc`
  (verified: tuples raise SignatureValidationError raw). Measure-level
  (HAVING) filters are NOT `metric()`'s job — use the fluent escape hatch;
  a `having=` kwarg can come later if demand appears. Unknown dim/measure
  names → `SemanticsError` listing what the model actually defines.
  (`to_pyarrow()` verified present; fall back to `execute()`→pandas if a BSL
  version lacks it.) Results flow through the frames boundary → polars /
  `defaults.frames`, not BSL's pandas default.
- **Ibis backend is private** (`ws._ibis()`, cached): its v1 job is plumbing
  for `models()`. Semantic tables already expose full Ibis; promote to a
  public verb only if real ad-hoc demand materialises.
- **Table binding by convention.** Bare `waitlist` → `main.waitlist`; dotted
  `files.finance_extract` → that schema/table. No binding config.
  Resolution detail (verified): ibis does NOT accept dotted names —
  `con.table("files.finance_extract")` raises TableNotFound; resolve by
  splitting and calling `con.table(name, database=schema)`. Reuse
  `_split_table()` from workspace.py, including its `_mirror`-schema block
  (models over mirror metadata are refused, consistent with `land()`).
- **Notebook-only.** No MCP server, no serving, no `wh metric` CLI in v1.
  `wh validate` grows a semantics check with an explicit policy:
  no semantics dir configured/present → skip silently; model files present
  but `[semantics]` extra missing → validate FAILS with the install command
  (silently skipping would let broken models pass CI, defeating the point);
  otherwise parse every model and resolve every table, failing with the
  file/model named.
- **Optional dependency.** `[semantics]` extra =
  `boring-semantic-layer>=0.3.15,<0.4` + `ibis-framework[duckdb]` (heavy,
  hence an extra; check ibis's own duckdb constraint is compatible with
  wh's core duckdb pin when defining it). BSL is 0.x — anything may churn,
  including the YAML schema, so the "YAML is the stable interface" rule is
  a working assumption, not a contract; upgrades are deliberate and
  `semantics.py` absorbs churn. All semantic imports are lazy inside
  `models()`/`metric()` via one small import function (so tests can fake
  its absence); missing extra → one clear sentence with the install command.
  (BSL exposes no `__version__`; use `importlib.metadata.version` if needed.)

## Package layout

```
src/wh/
  semantics.py     # _ibis plumbing, models(), metric(), validation helper
semantics/         # (analysis project, not this repo) *.yml model files
```

`Workspace` gains `models()` and `metric()`; module-level `wh.models()` /
`wh.metric()` delegate to the default workspace as usual. No submodule/verb
name collision (module `semantics`, verbs `models`/`metric`).

## Config: `wh.yaml`

```yaml
semantics:
  dir: ./semantics   # default; relative to wh.yaml; absent dir = no models (not an error)
```

Model content lives in the YAML files themselves (BSL's schema — unbound
`_.column` syntax; descriptions strongly encouraged, they surface in repr
and line up with the mirror's COMMENT ON metadata):

```yaml
# semantics/waitlist.yml
waitlist:
  table: waitlist                      # resolved against the mirror
  dimensions:
    specialty:
      expr: _.specialty
      description: "Clinical specialty of the referral"
    clinic: _.clinic_code
  measures:
    patients_waiting:
      expr: _.patient_ur.nunique()      # distinct patients — NOT _.count():
      description: "Distinct patients on the list at snapshot"   # the flagship
                                        # example must not conflate row count
                                        # with distinct count
    median_wait_days: _.wait_days.median()
```

## Loading

`wh.models()` scans `semantics/*.yml` AND `*.yaml` (sorted, deterministic —
missing either spelling silently loses models, the worst failure mode).
Loading is **merge-then-one-call**, not per-file: `yaml.safe_load` each file
(needed anyway for duplicate detection with file attribution and to collect
`table:` names), merge into one config dict, then a single
`bsl.from_config(merged, tables=...)` call. Rationale (verified): BSL
resolves `joins:` only among models in the same load call — per-file
`from_yaml` would make file organisation an invisible semantic boundary
where cross-file joins fail. Cache the result on the workspace;
`models(reload=True)` re-reads after editing YAML.

Load-time failures, all `SemanticsError(WhError)`:
- `[semantics]` extra not installed → install command in the message
- duplicate model name across files → names both files (BSL merges dicts
  silently; wh must detect this itself)
- unresolvable table → lists the tables the mirror actually has (BSL's own
  failure is a raw KeyError — unacceptable)

Query-time: `metric()` validates requested dims/measures against the model's
declared names first (BSL/Ibis errors list raw table columns, not semantic
names — verified misleading). Genuine expression-evaluation errors still
pass through.

## Interactions & constraints

- **Cache invalidation is self-keying, not hook-based.** `Workspace.con`
  self-heals (reopens a new connection object after close/death), so a
  mirror-hook-only invalidation leaves stale ibis backends on every other
  reopen path (manual `ws.close()`, etc.) — verified: a backend over a
  closed connection fails on every use. Instead the cache stores the
  connection object it was built from and rebuilds whenever
  `self.con is not cached_con` (models dict keyed likewise on the backend).
  Mirror-swap invalidation then falls out automatically; no hook in
  `mirror()` at all. Gotcha to document: materialised frames survive a
  refresh; un-executed lazy expressions do not (fail-loud with
  ConnectionException, verified) — re-fetch from `wh.models()`.
- **Committed models bind durable tables only.** `register()`ed frames DO
  bind (verified end-to-end — same connection handle), but registrations
  are connection-local and ephemeral: a committed YAML model over a
  registered name would permanently fail `wh validate` in CI. So the
  contract is: `semantics/*.yml` files reference mirror/`land()`ed tables;
  binding a registered frame is a session-level trick (register first, then
  `models(reload=True)`), not something validate must accept.
- **Version pinning.** BSL is young and moving. Floor-pin, upgrade
  deliberately, keep churn contained in `semantics.py`.
- **Quack / remote metrics.** Out of scope; different design if ever wanted.

## Testing

- **Unit** (no extra needed): `semantics.dir` config parsing, missing-extra
  error message, scan order + duplicate detection on fixture YAML.
- **DuckDB-real** (dev env installs the extra): fixture mirror + two model
  files → `models()` binds; `metric()` returns correct **values** in the
  preferred backend; the owned mapping specifically: raw-column filter
  through `metric()` (pre-aggregation placement) returns correct values,
  tuple `order_by` translation, unknown dim/measure → SemanticsError
  listing declared names; cross-file join loads (merge-then-one-call);
  dotted-schema binding; `register()`ed frame bound as a model table
  (session-level, post-`reload=True`); unresolvable-table error lists
  candidates; cache rebuild after `mirror()` AND after manual `close()`;
  `models(reload=True)` picks up edits.
- **Validation:** `wh validate` fails with a named file/model on a broken
  definition. No SQL Server involvement anywhere in this phase.

## Rollout (one phase, three tasks)

1. `[semantics]` extra + `_ibis()` + `models()` loader (merge-then-one-call,
   binding, errors, self-keying cache — no mirror hook needed).
2. `metric()` mapping through the frames boundary (filter placement, name
   validation, order_by translation).
3. `wh validate` semantics check + docs (README example, CLAUDE.md notes).

Later, only if earned: Python model escape hatch as a packaged convention,
freshness stamps on query results, BSL's chart helper.
