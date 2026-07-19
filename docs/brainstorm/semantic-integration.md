# semantics design (Boring Semantic Layer integration)

Add a semantic layer to `wh` so metric definitions live in one place and
every notebook computes the same numbers. Built on
[boring-semantic-layer](https://github.com/boringdata/boring-semantic-layer)
(BSL), which sits on Ibis: models are defined once, queried from notebooks,
and compile to SQL against the local mirror. Notebook-only for v1 — no
serving, no CLI query surface.

## Design principle

**Definitions in YAML, execution through the session connection, sugar on
top.** `wh` does not wrap what BSL does well — it loads models, binds them to
mirror tables, and returns frames. Anything BSL can express is reachable by
dropping down one layer; the sugar covers the 90% case in one line:

```python
import wh

models = wh.models()                       # all semantic models, bound to the mirror
df = wh.metric("waitlist",                 # one-liner: query → preferred frame
    measures=["patients_waiting", "median_wait_days"],
    dims=["specialty", "clinic"],
    filters=[...],
)
wl = models["waitlist"]                    # raw BSL semantic table when you
                                           # need windows, composition, .sql()
```

## Decisions made

- **Definitions are YAML** in a `semantics/` directory, loaded with BSL's
  `from_yaml`. Rationale: matches the config-driven grain of `wh.yaml`,
  git-diffable and reviewable by non-Python colleagues (the whole
  report-consistency point), and insulated from BSL's Python API churn
  (v1→v2 reworked it; YAML stayed stable). Python model definitions remain
  possible as an escape hatch but are not the packaged path in v1.
- **One DuckDB instance.** Ibis wraps the existing session connection via
  `ibis.duckdb.from_connection(ws.connect())` — never a second connection to
  the file (DuckDB forbids mixed-mode connections in-process, and a second
  read-write handle would break the swap semantics). Consequence:
  `wh.register(df, "cohort")` frames and `land()`ed scratch tables are
  bindable into models like any mirror table.
- **Both query layers.** `wh.metric()` for the one-liner (returns a frame via
  the existing narwhals boundary, respecting `defaults.frames`);
  `wh.models()[name]` exposes the untouched BSL object for everything else.
  `wh` implements no query semantics of its own.
- **Table binding by convention.** A table name in model YAML resolves to
  that name in the mirror: bare `waitlist` → `main.waitlist`; dotted
  `files.finance_extract` → that schema/table. No separate binding config.
- **Notebook-only.** No MCP server, no serving, no `wh metric` CLI in v1.
  `wh validate` grows a semantics check (models parse, tables resolve) since
  broken-on-load definitions should fail in CI, not mid-analysis.
- **Optional dependency.** `[semantics]` extra pulls
  `boring-semantic-layer` + `ibis-framework[duckdb]`. Core `wh` stays lean;
  importing `wh.models()` without the extra raises one clear sentence with
  the install command.

## Package layout

```
src/wh/
  semantics.py     # ibis_con(), models(), metric(), validation
semantics/         # (analysis project, not this repo) *.yml model files
```

`Workspace` gains `ibis_con()` and `models()`; module-level `wh.models()` /
`wh.metric()` delegate to the default workspace as usual.

## Config: `wh.yaml`

```yaml
semantics:
  dir: ./semantics        # default; created lazily, absence = no models
```

That's the whole config surface. Model content lives in the YAML files
themselves (BSL's schema — unbound `_.column` syntax):

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

Descriptions are strongly encouraged — they surface in `wh.models()` repr
and line up with the mirror's `COMMENT ON` metadata.

## Core API

**Plumbing:**

```python
ibis = wh.ibis_con()      # ibis DuckDB backend wrapping the session connection
```

Cached on the workspace. Exposed publicly because ad-hoc Ibis over the mirror
is useful on its own.

**Loading:**

```python
models = wh.models()             # dict[str, SemanticTable]; cached per session
models = wh.models(reload=True)  # re-read after editing YAML
```

Scans `semantics/*.yml` (sorted, deterministic), builds the `tables=` dict by
resolving each referenced name through `ibis_con()`, calls `from_yaml` per
file, merges. Duplicate model names across files → `SemanticsError` naming
both files. Unresolvable table → `SemanticsError` listing the tables the
mirror actually has (freshness meta already knows them).

**Querying:**

```python
df = wh.metric("waitlist", measures=[...], dims=[...], filters=[...])
```

Thin: look up the model, delegate to BSL's query method passing kwargs
through verbatim, execute, hand the result to the frames boundary. Filter
syntax, time grains, ordering — all BSL's, documented by pointing at BSL's
docs, not re-specified here.

**Escape hatch:**

```python
wl = wh.models()["waitlist"]     # BSL semantic table = Ibis expression
wl.group_by(...)...to_pyarrow()  # full Ibis surface; ibis.to_sql(expr) to
                                 # inspect the generated SQL
```

## Errors

`SemanticsError(WhError)` for load/binding problems (missing dir is not an
error; missing extra, duplicate model, unresolvable table are). Query-time
errors from BSL/Ibis pass through untouched — they name the offending
dimension/measure better than a wrapper would.

## Interactions & constraints

- **Refresh vs cached models.** Models hold Ibis table refs into the session
  connection. After `wh.mirror()` the swap replaces the file; the session
  connection and any loaded models must be considered stale.
  `wh.mirror()` already owns the workspace — it invalidates the cached
  connection, `ibis_con`, and models; next access rebuilds. Document the
  one gotcha: frames already materialised are fine, unexecuted lazy
  expressions are not.
- **Version pinning.** BSL is young and moving (v2 landed mid-2026). Pin a
  floor (`boring-semantic-layer>=0.x`) and treat upgrades as deliberate.
  The YAML files are the stable interface; `semantics.py` absorbs API churn.
- **Quack.** Out of scope. Models bind to the local file through the session
  connection. If a remote-metrics story is ever wanted, it's a different
  design (and blocked on Quack's catalog/schema maturity anyway).

## Testing

- **Unit** (no extra needed): config parsing, dir scanning order, duplicate
  detection, clear-error-when-extra-missing.
- **DuckDB-real** (skipped without `[semantics]` installed): fixture mirror +
  two-model fixture YAML → `models()` binds; `metric()` returns the
  preferred-backend frame with correct values; dotted-schema binding;
  unresolvable table error lists candidates; cache invalidation after
  `mirror()`; `register()`ed frame bindable as a model table.
- **Validation**: `wh validate` fails on a broken model file.

## Rollout

1. `[semantics]` extra + `ibis_con()` + `models()` loader with binding and
   errors.
2. `metric()` sugar through the frames boundary + cache invalidation hook in
   `mirror()`.
3. `wh validate` semantics check.
4. Later: Python model escape hatch as a packaged convention
   (`semantics/models.py`), freshness stamps on query results, chart helper
   (BSL has one) if it earns its keep.
