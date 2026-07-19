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
- Semantics phase COMPLETE (2026-07-19): BSL integration —
  `models()`/`model()`/`frame()`, `[semantics]` extra, validate check.
  Design: `docs/plans/2026-07-19-semantics-design.md` (adversarially
  reviewed); plan: `docs/plans/2026-07-19-semantics-phase.md`.

## Semantics notes

- wh owns NO query semantics: BSL's fluent API is the query language. An
  earlier metric() kwargs wrapper was designed and deliberately killed —
  do not reintroduce a query dialect.
- Loading is merge-then-one-call (`from_config` on all files merged):
  per-file `from_yaml` breaks cross-file join references. BSL 0.3.15
  DECLARED-join querying is broken (poisons all queries on a joining
  model); `test_join_dimension_query` is strict-xfail and will flag the
  fixing release. QUERY-TIME joins work fully:
  `wl.join_one(other, on=lambda l, r: l.raw_col == r.raw_col)` — on= gets
  RAW tables, and dims are model-prefixed afterwards ("wl.specialty").
  Do NOT build a shim replaying YAML joins: through join_one (query
  dialect); recommend mirror-level pre-joins for centralised joins.
  Upstream issue draft: docs/upstream/bsl-join-query-issue.md (not yet
  filed — user to approve). Also to file: raw-column error messages;
  case-only rename bug below.
- Case-only NAME collisions break BSL/ibis execution with obscure schema
  errors — and it's broader than renames: any dim/measure name colliding
  case-insensitively with ANY table column (even computed exprs). Two
  guards: `merge_model_files` lints simple `_.Col` renames (no binding
  needed), `_check_name_collisions` covers everything at bind time with
  real columns in hand. Keep exact column case or a genuinely different name.
- Caches are SELF-KEYING on connection/backend object identity (`ws.con is
  cached_con`). Never add invalidation hooks — they miss reopen paths.
- Module `semantics.py` vs verbs `models/model/frame`; `frames.py` module
  vs `frame` verb — names differ deliberately (shadowing gotcha).
- BSL is 0.x, pinned `>=0.3.15,<0.4`; churn (including YAML) is absorbed
  in semantics.py only. Upgrades are deliberate.

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
