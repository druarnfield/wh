# wh semantics design (Boring Semantic Layer integration)

**Date:** 2026-07-19
**Status:** agreed (brainstorming session; refines `docs/brainstorm/semantic-integration.md`)

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
- **`metric()` owns the mapping.** Its entire implementation:

  ```python
  q = self.models()[model]        # unknown model → SemanticsError listing models
  if dims:     q = q.group_by(*dims)
  q = q.aggregate(*measures)
  for f in filters:  q = q.filter(f)
  if order_by: q = q.order_by(...)
  if limit:    q = q.limit(limit)
  return from_arrow(q.to_pyarrow(), self._backend(backend))
  ```

  (`to_pyarrow()` because semantic queries are Ibis expressions; fall back to
  `execute()`→pandas if a BSL version lacks it.) Results flow through the
  frames boundary → polars / `defaults.frames`, not BSL's pandas default.
- **Ibis backend is private** (`ws._ibis()`, cached): its v1 job is plumbing
  for `models()`. Semantic tables already expose full Ibis; promote to a
  public verb only if real ad-hoc demand materialises.
- **Table binding by convention.** Bare `waitlist` → `main.waitlist`; dotted
  `files.finance_extract` → that schema/table. No binding config.
- **Notebook-only.** No MCP server, no serving, no `wh metric` CLI in v1.
  `wh validate` grows a semantics check (models parse, tables resolve) so
  broken definitions fail in CI, not mid-analysis.
- **Optional dependency.** `[semantics]` extra =
  `boring-semantic-layer` + `ibis-framework[duckdb]` (heavy, hence an
  extra). All semantic imports are lazy inside `models()`/`metric()`;
  missing extra → one clear sentence with the install command. Pin a version
  floor on BSL; upgrades are deliberate. Maintenance rule for CLAUDE.md:
  the YAML files are the stable interface, `semantics.py` absorbs API churn.

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
      expr: _.count()
      description: "Distinct patients on the list at snapshot"
    median_wait_days: _.wait_days.median()
```

## Loading

`wh.models()` scans `semantics/*.yml` (sorted, deterministic). Per file:
collect referenced `table:` names, resolve each through `_ibis()`, call
`from_yaml(path, tables=...)`, merge into one dict, cache on the workspace.
`models(reload=True)` re-reads after editing YAML.

Load-time failures, all `SemanticsError(WhError)`:
- `[semantics]` extra not installed → install command in the message
- duplicate model name across files → names both files
- unresolvable table → lists the tables the mirror actually has

Query-time errors from BSL/Ibis pass through untouched — they name the
offending dimension/measure better than a wrapper would.

## Interactions & constraints

- **Refresh vs cached models.** `wh.mirror()` already closes the session
  connection before the swap; it now also drops the cached Ibis backend and
  models dict — next access rebuilds against the fresh file. Gotcha to
  document: materialised frames survive a refresh; un-executed lazy
  expressions do not — re-fetch from `wh.models()` after mirroring.
- **Version pinning.** BSL is young and moving. Floor-pin, upgrade
  deliberately, keep churn contained in `semantics.py`.
- **Quack / remote metrics.** Out of scope; different design if ever wanted.

## Testing

- **Unit** (no extra needed): `semantics.dir` config parsing, missing-extra
  error message, scan order + duplicate detection on fixture YAML.
- **DuckDB-real** (dev env installs the extra): fixture mirror + two model
  files → `models()` binds; `metric()` returns correct **values** in the
  preferred backend; dims/filters/order_by/limit mapping; dotted-schema
  binding; `register()`ed frame bound as a model table; unresolvable-table
  error lists candidates; cache invalidation after `mirror()`;
  `models(reload=True)` picks up edits.
- **Validation:** `wh validate` fails with a named file/model on a broken
  definition. No SQL Server involvement anywhere in this phase.

## Rollout (one phase, three tasks)

1. `[semantics]` extra + `_ibis()` + `models()` loader with binding, errors,
   and mirror-invalidation hook.
2. `metric()` mapping through the frames boundary.
3. `wh validate` semantics check + docs (README example, CLAUDE.md notes).

Later, only if earned: Python model escape hatch as a packaged convention,
freshness stamps on query results, BSL's chart helper.
