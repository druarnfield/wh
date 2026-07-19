# warehouse-tools design

**Date:** 2026-07-19
**Status:** agreed (brainstorming session)

A helper library for getting data in and out of DuckDB for local analysis.
Sources: SQL Server (the warehouse), Excel (messy business files), CSV; Oracle
later. Destination: a local `.duckdb` mirror (native tables or parquet+views)
used as the connection in marimo/jupyter. Plus writeback of analysis results
to SQL Server.

## Design principle

**As simple as possible by default, with overrides for complexity.** The API
is five memorable verbs at module level that all "just work" from the
discovered config:

```python
import wh

con = wh.connect()                  # duckdb connection to the local mirror
df  = wh.pull("SELECT ...")         # ad-hoc query against the warehouse
wh.push(df, "Sandbox.dbo.results")  # publish results back to SQL Server
df  = wh.read_excel("messy.xlsx")   # smart Excel reader
wh.mirror()                         # full refresh of the local mirror
```

Everything else — named sources, backends, modes, staging details — is a
keyword override or a `Workspace` method. If a call needs more than one line
in the common case, the design is wrong.

Module-level functions delegate to a default `Workspace` (created lazily from
the discovered `wh.yaml`). `ws = wh.workspace(...)` is the explicit form for
multiple configs/workspaces.

## Decisions made

- **Library-first**, thin CLI on top (`wh mirror`, `wh validate`).
- **v1 sources:** SQL Server + Excel/CSV. Oracle later (same config shape,
  `driver: oracle`).
- **Writeback:** create/replace tables only (v1). Append/upsert later.
- **Excel:** smart reader (auto header detection) + composable cleaners,
  driven per-file in the notebook. Recurring files can be promoted to config.
- **Frames:** narwhals at the boundaries; Arrow/DuckDB in the core. No hard
  pandas/polars dependency. `pull()` returns the preferred backend
  (config `frames:`; default polars if installed, else pandas, else pyarrow).
- **Packaging:** standalone installable package (src layout, extras), pulled
  into analysis projects via uv (git URL or path).
- **Push safety:** config allowlist of writable database/schemas; `push()`
  refuses everything else.
- **Connections:** structured named sources in config — never hand-write a
  connection string. Secrets only via env vars.

## Package layout

```
src/wh/
  __init__.py      # public API: connect, pull, push, read_excel, read_csv,
                   # clean, mirror, workspace
  config.py        # wh.yaml loading/validation
  workspace.py     # Workspace — the hub everything hangs off
  frames.py        # narwhals boundary: any-frame → Arrow → preferred backend
  sources/
    mssql.py       # Arrow extraction (lifted from mirror.py POC)
    excel.py       # smart reader + cleaners
    files.py       # csv/parquet convention helpers
  mirror.py        # config-driven refresh + atomic swap
  push.py          # writeback with schema allowlist
  cli.py           # `wh mirror`, `wh validate`
```

Dependencies: core = `duckdb`, `pyarrow`, `narwhals`, `pyyaml`,
`mssql-python`. Extras: `[excel]`, `[polars]`, later `[oracle]`.

## Config: `wh.yaml`

Discovered by walking up from the cwd (notebooks anywhere in a project just
work), or passed explicitly. Superset of the POC config:

```yaml
sources:
  warehouse:                    # first source = default for pull/push
    driver: mssql
    server: localhost,1433
    database: ExecReporting
    encrypt: false              # defaults to true
    trust_server_certificate: true
    auth:
      user: sa
      password_env: WH_WAREHOUSE_PWD   # secrets stay in env vars
      # or: trusted: true              # Windows auth
    # dsn_env: MSSQL_DSN        # escape hatch: full connection string from env

destination:
  duckdb_path: ./metrics.duckdb
  parquet_dir: ./parquet

push:
  allow:                        # push() refuses anything not listed
    - Sandbox.dbo
    - Sandbox.analysis

defaults:
  mode: native                  # native | parquet
  schema_in_duckdb: main
  compression: zstd
  compression_level: 9
  batch_size: 100000
  frames: polars                # preferred return backend

tables:                         # mirror specs (as POC: name, source
  - name: outpatient_waitlist_current      # table-ref or query, mode,
    source: {database: ExecReporting, schema: dbo, table: OutpatientWaitsCurrent}
    description: "Current Outpatient Waitlist"

files:                          # recurring Excel/CSV that land during refresh
  - name: finance_extract
    path: ./inbox/finance_*.xlsx
    sheet: Data
    schema_in_duckdb: files
```

The library assembles connection strings from structured fields; `dsn_env`
remains for anything the fields don't cover.

## Core API

**Reading (local-first).** Day-to-day analysis hits the local mirror through
plain DuckDB — the library doesn't wrap what DuckDB does well.
`con.sql("...")` relations already have `.pl()`, `.df()`, `.arrow()`.

**Pulling from the warehouse (ad-hoc):**

```python
df = wh.pull("SELECT ... WHERE ...")               # → preferred-backend frame
df = wh.pull(sql, source="other_server")           # named source override
wh.land(sql, table="scratch.referrals_raw")        # stream straight into the
                                                   # .duckdb (constant memory)
```

**Registering local frames** — any frame becomes queryable next to mirror
tables:

```python
wh.register(df, "cohort")     # temp view for this session
con.sql("SELECT * FROM cohort JOIN main.waitlist USING (ur)")
```

**Pushing results back:**

```python
wh.push(results, "Sandbox.dbo.waitlist_analysis")                        # create
wh.push(results, "Sandbox.dbo.waitlist_analysis", if_exists="replace")   # idempotent
```

Accepts any narwhals-compatible frame or a DuckDB relation → Arrow →
SQL Server. Refuses non-allowlisted schemas (`PushRefused` names the allowed
ones). `if_exists`: `fail` (default) | `replace`. Replace = drop+create+insert
in one transaction. One documented Arrow→SQL Server dtype table
(strings→NVARCHAR, timestamps→DATETIME2, ...).

## Mirror & refresh

Keeps the POC semantics: staging build, native/parquet modes, parquet-backed
views, atomic swap, all-or-nothing failure (live files untouched until
success). Upgrades:

1. **`--only` carries over.** Staging attaches the current mirror and copies
   the tables not being refreshed, then re-extracts the selected ones. Result
   is always a complete mirror. Falls back to full build if none exists.
2. **`_mirror.meta` table.** Per table: source, mode, row count, extracted_at,
   duration, config hash. `wh.freshness()` in notebooks; printed after CLI
   refresh; enables `wh mirror --stale 24h`.
3. **`files:` entries land during refresh** alongside warehouse tables.

## Excel/CSV

```python
raw = wh.read_excel(path)                      # header="auto" default
raw = wh.read_excel(path, sheet="Data", header=(3, 4))   # multi-row headers
```

`header="auto"` scans the first ~20 rows for the most plausible header row
(mostly non-empty, mostly strings, distinct). Merged header cells
forward-filled; multi-row headers joined. Explicit `header=`/`skip_rows`
always available.

Cleaners are composable functions on narwhals frames (work on any backend):

```python
df = wh.clean(raw,
    wh.clean.snake_names,        # "Referral Date " → referral_date
    wh.clean.drop_empty,         # all-null rows/cols
    wh.clean.strip_strings,
    wh.clean.parse_dates("referral_date", "seen_date"),  # incl. Excel serials
    wh.clean.numeric("wait_days"),                       # "1,234", "$5", "-" → null
)
```

CSV: `wh.read_csv(path)` delegates to DuckDB's sniffing reader. Both readers
take `land="schema.table"` to write into the workspace DuckDB instead of
returning a frame.

## Errors

All inherit `WhError`: `ConfigError`, `SourceError` (wraps driver errors,
names source/table), `PushRefused`, `SchemaMismatch`. Notebook-friendly: one
clear sentence + the fix, not a driver stack trace.

## Testing

- **Unit** (no external deps): config parsing, connection-string assembly,
  header detection + cleaners on fixture files, dtype mapping, allowlist.
- **DuckDB-real** (embedded, runs everywhere): mirror build/swap/carry-over,
  `land()`, `register()`, `_mirror.meta`.
- **Integration** (opt-in via `WH_TEST_DSN`): pull/push/mirror round-trips
  against local SQL Server. Skipped otherwise.

## Rollout

1. Package skeleton + config (named sources) + `Workspace`/`connect()` +
   mirror refactor (meta table, carry-over `--only`).
2. `pull()` / `land()` / `register()` + narwhals frame boundary.
3. `push()` with allowlist.
4. Excel/CSV readers + cleaners.
5. Later: Oracle source, append/upsert push modes.
