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
- Next: phase 4 (Excel smart reader + cleaners, CSV helpers) — needs its own
  plan. Later: Oracle source, append/upsert push.

## Phase 3 notes

- push targets are three-part (`Database.schema.table`) to match allowlist
  entries (`Database.schema`); comparisons case-insensitive.
- The Arrow→SQL Server type map is the docstring of `src/wh/push.py` — keep
  code and docstring in sync.
- `push_arrow()` takes ANY DB-API connection (that's the unit-test seam);
  allowlist is checked in `Workspace.push()` BEFORE a connection is opened.
- `attatch.sql` / `start.sql` at repo root are the user's own scratch files —
  leave them alone.

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
