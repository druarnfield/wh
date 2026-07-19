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

**Definitions in YAML, execution through the session connection, zero query
semantics in wh.** BSL's query surface is fluent
(`model.group_by(...).aggregate(...)`) — its authors deleted the v1 kwargs
form deliberately, and an earlier draft of this design resurrected it as
`wh.metric()`; adversarial review showed every substantive flaw concentrated
in that mapping (filter placement, order_by shape, error interception): the
signature of building a lagging dialect. So `wh` wraps nothing about
querying. Its sugar is model lookup and frame conversion only:

```python
import wh
from ibis import _

wl = wh.model("waitlist")            # lookup; unknown name → SemanticsError
                                     # listing available models
df = wh.frame(                       # ANY frame-ish thing → preferred backend
    wl.filter(_.wait_days > 30)      # 100% BSL fluent API — filters, time
      .group_by("specialty")         # grains, joins, HAVING, windows, .sql()
      .aggregate("patients_waiting") # — nothing mapped, nothing lagging
)
models = wh.models()                 # the full dict when you want it
```

`wh.frame(obj, backend=None)` is generic, not semantics-specific: it converts
BSL query expressions and Ibis expressions (duck-typed on `.to_pyarrow()`,
verified present), plus everything `frames.to_arrow` already accepts
(pandas/polars frames, DuckDB relations, Arrow) to the preferred backend.
One reusable concept instead of one bespoke query dialect.

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
- **No `metric()`; `model()` + `frame()` instead.** `wh.model(name)` is
  `models()[name]` with a `SemanticsError` listing available models on a
  miss. `wh.frame(obj, backend=None)` = `to_arrow` (extended to duck-type
  `.to_pyarrow()`) → `from_arrow` with the usual backend resolution. Query
  construction is entirely BSL's fluent API; `wh` maintains no mapping, so
  BSL feature growth (time grains, HAVING, windows) is available on day one
  and BSL API churn surfaces visibly in notebooks (a find-replace) rather
  than silently in a compatibility shim. Known trade-off: BSL's query-time
  errors reach users undiluted, and its bad-name error lists raw table
  columns rather than semantic names (verified) — that's an upstream issue
  to file, not a wrapper to build; model discovery is served by the model
  repr (dims/measures with descriptions).
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
  `models()`/`model()` via one small import function (so tests can fake
  its absence); missing extra → one clear sentence with the install command.
  (BSL exposes no `__version__`; use `importlib.metadata.version` if needed.)

## Package layout

```
src/wh/
  semantics.py     # _ibis plumbing, models(), model(), validation helper
semantics/         # (analysis project, not this repo) *.yml model files
```

`Workspace` gains `models()`, `model()`, and `frame()`; module-level
`wh.models()` / `wh.model()` / `wh.frame()` delegate to the default
workspace as usual. No submodule/verb name collision (module `semantics`,
verbs `models`/`model`/`frame`; note module `frames.py` vs verb `frame` —
different names, safe, but keep it that way).

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

Query-time errors are BSL's, undiluted (no wrapper exists to intercept
them). The known wart — bad dim/measure names produce an error listing raw
table columns instead of semantic names — is an upstream issue to file
against BSL, mitigated locally by the model repr showing declared
dims/measures with descriptions.

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
  files → `models()` binds; a fluent BSL query through `wh.frame()` returns
  correct **values** in the preferred backend (and `backend=` overrides);
  `wh.model("nope")` → SemanticsError listing available models;
  cross-file join loads (merge-then-one-call); dotted-schema binding;
  `register()`ed frame bound as a model table (session-level,
  post-`reload=True`); unresolvable-table error lists candidates; cache
  rebuild after `mirror()` AND after manual `close()`;
  `models(reload=True)` picks up edits.
- **frames-boundary unit tests** (no extra needed): `to_arrow` duck-types
  any object with `.to_pyarrow()`; `wh.frame()` on plain frames/relations
  behaves as a generic converter.
- **Validation:** `wh validate` fails with a named file/model on a broken
  definition. No SQL Server involvement anywhere in this phase.

## Rollout (one phase, three tasks)

1. `[semantics]` extra + `_ibis()` + `models()`/`model()` loader
   (merge-then-one-call, binding, errors, self-keying cache — no mirror
   hook needed).
2. `wh.frame()` + the `.to_pyarrow()` duck-type in `frames.to_arrow`.
3. `wh validate` semantics check + docs (README example, CLAUDE.md notes,
   upstream issue filed against BSL for the raw-column error message).

Later, only if earned: Python model escape hatch as a packaged convention,
freshness stamps on query results, BSL's chart helper.
