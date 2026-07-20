# warehouse-tools (`wh`)

Helper library for local data analysis: mirror warehouse data (SQL Server) into
a local DuckDB file, analyse it in marimo/jupyter, push results back to
SQL Server. Excel/CSV readers for messy business files. Oracle later.

> **Keep this file current.** Update it in the same commit as the change that
> makes it stale — new commands, moved files, changed conventions, finished
> phases. Review it at the start of any substantial piece of work. This is my
> working reference, not user documentation (that's README.md).

## Current state

- Phase 1 COMPLETE (2026-07-19): `src/wh/` package with config/discovery,
  Workspace, connect/mirror/freshness, carry-over `--only`, `_mirror.meta`,
  MssqlExtractor, CLI. Post-review hardening applied (see phase 1 plan
  amendments). Verified end-to-end against the local dev server.
- Phase 2 COMPLETE (2026-07-19): `pull()` / `land()` / `register()`, narwhals
  frame boundary (`frames.py`), `defaults.frames` config, shared session
  connection (`ws.con`), `MssqlExtractor.query()`. Plan:
  `docs/plans/2026-07-19-warehouse-tools-phase2.md`.
- Phase 3 COMPLETE (2026-07-19): `push()` writeback — `push.allow` config,
  pure helpers + transactional `push_arrow()` over any DB-API conn (fake-conn
  unit tests), `open_connection()` shared with the extractor. Plan:
  `docs/plans/2026-07-19-warehouse-tools-phase3.md`.
- Phase 4 COMPLETE (2026-07-19): `read_excel` (calamine, auto/multi-row
  headers), `clean` + cleaners (`cleaning.py`), `read_csv`, `land=` on both
  readers, configless module-level fallback. Plan:
  `docs/plans/2026-07-19-warehouse-tools-phase4.md`.
- Design fully delivered. Later: Oracle source, append/upsert push.
- BSL semantic layer: built 2026-07-19, REMOVED 2026-07-20 (rejected —
  not usable, design disagreed with). Upstream-bug knowledge preserved
  in `docs/upstream/` and git history of `docs/plans/2026-07-19-semantics-*`.
  `errors.SemanticsError` and `config.semantics_dir` survive for the
  new layer.
- Metrics phase 1 COMPLETE (2026-07-20): rollout steps 0–1 of
  `docs/plans/2026-07-20-metrics-design.md` (plan:
  `docs/plans/2026-07-20-metrics-phase1.md`). Delivered: `wh/metrics/`
  package (loader / context_ops / timegrain / compiler / result /
  checks), verbs `wh.model`/`wh.slice`/`wh.context`/`not_`/`last`/`all`,
  two-lane compilation, snapshot global-max as-at, ratios, fiscal
  grains, strict context, bind-time checks, `wh validate` hook, canary
  invariant + lane-isolation test suites. Submodules never named after
  verbs (`model`/`slice`/`context`/`frame` — shadowing gotcha).
  Post-review hardening applied (see the fix commits of 2026-07-20).
- Metrics phase 2 COMPLETE (2026-07-20): `compare=` (prior/yoy/fytd as
  shifted CTEs self-joined back — never lag; fytd recomputed from base;
  per-CTE as-at on snapshot models), `complete_periods` (cadence-aware
  on snapshot models), `.suppress(n)` (hidden __cell_n per CTE; ratios
  null when their den < n). Plan:
  `docs/plans/2026-07-20-metrics-phase2.md`.
- Metrics phase 3 COMPLETE (2026-07-20): provenance —
  `Slice.provenance()` (measures+hashes, context buckets incl.
  empty-selection, shape incl. non-additive + strictness-weakened,
  schema fingerprints, _mirror.meta refresh stamps with honest absence,
  scan coverage vs fact min, per-lane as-at, truncation note, DuckDB
  version), semantic-projection measure hashes + model hashes in
  `wh/metrics/provenance.py`. Plan:
  `docs/plans/2026-07-20-metrics-phase3.md`.
- Metrics phase 4 COMPLETE (2026-07-20): `BoundModel.values()` (+
  `compile_values` — unscoped shared dims read the DIM TABLE only, no
  fact scan; scoped goes through the lanes, no as-at, NULL never an
  option), `filter_dim()` (mo.ui.multiselect; empty selection →
  unfiltered via the existing widget semantics), `filter_date()`
  (mo.ui.date_range over real fact bounds; .value fits time= exactly).
  marimo is a lazy import (dev dep only); exclude-your-own-field is
  `context=ctx.without(...)` — visible composition, never magic. Plan:
  `docs/plans/2026-07-20-metrics-phase4.md`.
  DESIGN FULLY DELIVERED (steps 0-4). Later per design: curated-schema
  git hash stamp, `wh.compose()` for cross-fact derived numbers,
  complementary suppression, context YAML round-trip.
- Hash-stability contract: `_project()` in provenance.py KEEPS a known
  scalar-key set and drops everything else — new serializer keys in a
  DuckDB upgrade can't shift hashes; only structural renames could, and
  the stamped duckdb version explains those. Don't "improve" it to
  keep-all-minus-noise; the allowlist IS the stability mechanism.

## Metrics-layer notes (design invariants — keep these true)

- Case-insensitive name collisions against real table columns caused
  obscure engine errors in the BSL era; the new layer's bind-time checks
  should keep guarding names with real columns in hand.
- Caches are SELF-KEYING on connection object identity (`ws.con is
  cached_con`) plus YAML mtimes. Never add invalidation hooks — they
  miss reopen paths.
- Two lanes are the guarantee: intrinsic `where` → `FILTER (WHERE ...)`,
  context → outer `WHERE`; on snapshot models the as-at subquery carries
  time predicates only. Tests assert this on parse trees with canary
  literals — never on SQL text.
- `json_serialize_sql` trees carry `query_location` keys that vary with
  whitespace — `tests/metrics/treecheck.py` strips any `*location*` key
  before comparing. Column refs are dicts with a `column_names` list.
- tests/metrics helper code lives in uniquely-named modules
  (`treecheck.py`, `fixtures_data.py`), NEVER imported from conftest —
  two `conftest.py` files on sys.path make `import conftest` ambiguous.
- Unresolved contexts (widgets, `wh.last`) resolve at slice time,
  anchored to `max(time_column)` of the fact — mirror data, never wall
  clock. Hash/serialise/provenance are defined over resolved contexts
  only.
- `datetime` IS a `date` subclass — `_lit` must test datetime BEFORE
  date or timestamps silently render as `DATE '...'` and DuckDB floors
  them (this moved snapshot as-at moments a week early pre-review).
  Time ranges compile day-inclusive (`>= lo AND < hi + 1 day`), never
  `BETWEEN ... DATE 'hi'`.
- Aggregate detection trick: `SELECT <expr> FROM fact WHERE 1=0` yields
  exactly 1 row for aggregates, 0 for per-row exprs (which GROUP BY ALL
  would silently turn into grouping columns).
- YAML names/columns are spliced into SQL unquoted — loader validates
  them as plain identifiers (`fact`/`period`/`time` + `__*` reserved).
  Adversarial review (2026-07-20) proved the injection: a measure named
  `"n, wait_days AS smuggled"` regrouped a total into per-row output.

## Phase 4 notes

- `cleaning.py` is deliberately NOT named `clean.py` (submodule/verb shadowing
  gotcha); `sources/excel.py` ≠ `read_excel` for the same reason.
- Excel mixed-type columns degrade to ALL-string by design (calamine floats
  render "8" not "8.0" via `_cell_str`); `clean.numeric` recovers numbers.
- Header forward-fill (merged cells) applies to all header rows EXCEPT the
  last — empties in the last/only header row mean "unnamed col" → col_N.
- `land=` on readers is raw replace semantics (blank rows included); clean
  first and use `register()`/`push()` if you want cleaned data persisted.
- Module-level `read_excel`/`read_csv` work with no wh.yaml (frame path only);
  `land=` without a config raises ConfigError. A wh.yaml that EXISTS but
  fails to parse always raises — never silently fall back (`_optional_workspace`).
- header="auto" heuristic caveat: a header row with several unnamed columns
  can score below a fully-populated all-string data row (ties prefer the
  earlier row, so this needs strict inequality to bite). The escape hatch is
  explicit `header=N`; don't try to make the heuristic perfect.
- `clean.numeric` NULLS anything non-numeric after currency stripping
  ("N/A", "TBC") by design — strict casting would raise raw backend errors
  naming the wrong column.

## Phase 3 notes

- push targets are three-part (`Database.schema.table`) to match allowlist
  entries (`Database.schema`); comparisons case-insensitive.
- The Arrow→SQL Server type map is the docstring of `src/wh/push.py` — keep
  code and docstring in sync.
- `push_arrow()` takes ANY DB-API connection (that's the unit-test seam);
  allowlist is checked in `Workspace.push()` BEFORE a connection is opened.
- `attatch.sql` / `start.sql` at repo root are the user's own scratch files —
  untracked and gitignored on purpose; leave them alone.

## Key documents

- `docs/plans/2026-07-19-warehouse-tools-design.md` — agreed design. Read first.
- `docs/plans/2026-07-19-warehouse-tools-phase1.md` — current implementation plan.

## Commands

- `uv run pytest` — full suite; SQL Server integration tests auto-skip
- `WH_TEST_DSN='Server=localhost,1433;Database=ExecReporting;UID=sa;PWD=...;Encrypt=no;TrustServerCertificate=yes;' uv run pytest` — include integration tests (local dev server)
- `uv run wh validate` / `uv run wh mirror [--only <table>]` — CLI
- Dev SQL Server: localhost,1433, database ExecReporting; password lives in
  `WH_WAREHOUSE_PWD` (ask the user; never write it into tracked files).

## Gotchas learned in phase 1

- Submodule/verb name collisions (`wh.mirror`, `wh.push`, `wh.workspace`) bite
  BOTH ways: `from . import mirror` returns the function, and a submodule's
  first (lazy) initialisation clobbers the same-named function on the package
  (wh.push broke this way — worked once, then became a module). Rules: use
  `from .mod import name` style inside the package, and initialise colliding
  submodules eagerly in `__init__.py` before the verb definitions (see the
  `_push_submodule` import there; test_module_verbs_survive_submodule_imports
  guards it).
- DuckDB: attached catalogs have no `prev.information_schema`; use
  `duckdb_tables() WHERE database_name = 'prev'`. TIMESTAMPTZ results need
  pytz — `_mirror.meta.extracted_at` is plain TIMESTAMP (UTC) instead.
  `.arrow()` on a result is a lazy reader; use `.to_arrow_table()`.
- `yaml.safe_dump` sorts keys — tests asserting "first source is default"
  must dump with `sort_keys=False`.
- Bash: `pytest | tail` swallows pytest's exit code; `set -o pipefail` before
  chaining `&& git commit`.

## Gotchas learned in phase 2

- DuckDB refuses read-only + read-write connections to the same file in one
  process ("different configuration" ConnectionException). Hence the shared
  session connection `ws.con`; NEVER `connect(read_only=True)` in-process.
  Second read-write connection is fine (`connect(fresh=True)`).
- `ws.mirror()` closes the session connection before the swap (old handle
  would serve pre-refresh data); it reopens lazily on next `ws.con` access.
- `land()` registers the streaming Arrow reader directly with DuckDB
  (constant memory); `pull()` materialises. Don't "simplify" land through
  `frames.to_arrow` — that would read everything into RAM (a test guards this).
- Landed tables are scratch BY DESIGN: `mirror()` rebuilds the .duckdb from
  config alone, so a refresh wipes anything `land()`ed. Documented in the
  land docstring + README; keep it documented if either changes.

## Architecture (see design doc for full detail)

- **Simple-by-default API**: five module-level verbs — `wh.connect() / pull() /
  push() / read_excel() / mirror()` — that just work from a discovered
  `wh.yaml`; `Workspace` + kwargs are the escape hatch. Don't grow the surface.
- **Config**: `wh.yaml` found by walking up from cwd; relative paths resolve
  against the config file's directory, NOT cwd. Named sources with structured
  fields; secrets only via env vars (`password_env`, `dsn_env` escape hatch).
- **Mirror**: staging build → atomic swap; live .duckdb/parquet never touched
  on failure. Extraction goes through a seam `extract(spec) -> Arrow` so all
  mirror logic tests with fake Arrow data — only `MssqlExtractor` needs a server.
- **`_mirror.meta`**: per-table freshness (row_count, extracted_at, duration).
- **Frames**: narwhals at the boundaries (phase 2+), Arrow/DuckDB in the core;
  no hard pandas/polars dependency.

## Conventions

- NO worktrees — never suggest or create them. Dru works on ordinary
  branches in the main checkout; feature work happens right here.
- TDD, strictly: failing test → verify fail → implement → verify pass → commit.
- Never mention Claude in commit messages.
- Notebook-friendly errors: all inherit `WhError`; one clear sentence + the fix.
- YAGNI: config keys and API params are added in the phase that uses them.

## Roadmap

Phase 1: skeleton, config, Workspace/connect, mirror refactor (in progress).
Phase 2: `pull()` / `land()` / `register()` + narwhals boundary.
Phase 3: `push()` with schema allowlist.
Phase 4: Excel smart reader + cleaners, CSV helpers.
Later: Oracle source, append/upsert push.
