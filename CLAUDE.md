# warehouse-tools (`wh`)

Helper library for local data analysis: mirror warehouse data (SQL Server) into
a local DuckDB file, analyse it in marimo/jupyter, push results back to
SQL Server. Excel/CSV readers for messy business files. Oracle later.

> **Keep this file current.** Update it in the same commit as the change that
> makes it stale — new commands, moved files, changed conventions, finished
> phases. Review it at the start of any substantial piece of work. This is my
> working reference, not user documentation (that's README.md).

## Current state

- Phase 1 in progress: executing `docs/plans/2026-07-19-warehouse-tools-phase1.md`
  task-by-task (TDD, commit per task). Update the line above as tasks land.
- `mirror.py` + `config.yaml` at repo root are the POC — deleted in Task 13,
  replaced by the `src/wh/` package and `wh.yaml`.

## Key documents

- `docs/plans/2026-07-19-warehouse-tools-design.md` — agreed design. Read first.
- `docs/plans/2026-07-19-warehouse-tools-phase1.md` — current implementation plan.

## Commands

- `uv run pytest` — full suite; SQL Server integration tests auto-skip
- `WH_TEST_DSN='Server=localhost,1433;Database=ExecReporting;UID=sa;PWD=...;Encrypt=no;TrustServerCertificate=yes;' uv run pytest` — include integration tests (local dev server)
- `uv run wh validate` / `uv run wh mirror [--only <table>]` — CLI (from Task 12)
- Dev SQL Server: localhost,1433, database ExecReporting; password lives in an
  env var (`WH_WAREHOUSE_PWD` once wh.yaml lands), never in config files.

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
