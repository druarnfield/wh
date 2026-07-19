# warehouse-tools (`wh`)

Helpers for local data analysis: mirror warehouse data (SQL Server) into a
local DuckDB file, analyse it in marimo/jupyter, push results back. Simple by
default — five verbs that just work — with overrides for everything else.

```python
import wh

con = wh.connect()      # session duckdb connection to the local mirror
wh.mirror()             # refresh the mirror from the warehouse
wh.freshness()          # how stale is each table?

df = wh.pull("SELECT ...")                    # ad-hoc warehouse query -> frame
wh.pull("SELECT ...", backend="pandas")       # or pick the frame library
wh.land("SELECT ...", table="scratch.raw")    # big pulls: stream into the .duckdb
                                              # NB: landed tables are scratch —
                                              # wh.mirror() rebuilds from config
                                              # and wipes them
wh.register(df, "cohort")                     # any frame queryable in SQL:
con.sql("SELECT * FROM cohort JOIN main.waitlist USING (ur)")

# coming in later phases:
wh.push(df, "Sandbox.dbo.results")       # publish results back
df = wh.read_excel("messy.xlsx")         # smart Excel reader
```

`pull()` returns polars if installed (else pandas, else pyarrow); set
`defaults.frames` in `wh.yaml` to pin it. `wh.connect()` returns a shared
session connection — DuckDB forbids mixing read-only and read-write
connections to one file in a process, so don't open your own read-only ones;
`wh.connect(fresh=True)` gives an independent read-write connection.

## Install

Into an analysis project:

```bash
uv add git+<this-repo-url>          # or: uv add --editable /path/to/checkout
```

## Configure

Drop a `wh.yaml` at your project root (found automatically from any subdir):

```yaml
sources:
  warehouse:
    driver: mssql
    server: myserver,1433
    database: Reporting
    auth:
      user: sa
      password_env: WH_WAREHOUSE_PWD   # secrets live in env vars, never here
      # or: trusted: true              # Windows auth

destination:
  duckdb_path: ./metrics.duckdb

tables:
  - name: waitlist
    schema_in_duckdb: main
    source: {database: Reporting, schema: dbo, table: Waitlist}
  - name: snapshot
    mode: parquet                      # parquet file + DuckDB view
    source:
      query: SELECT * FROM [Reporting].[dbo].[Snapshots] WHERE ...
```

See `wh.yaml` in this repo for a fuller example.

## CLI

```bash
wh validate                 # parse the config and exit
wh mirror                   # full refresh (staging build, atomic swap)
wh mirror --only waitlist   # refresh one table; the rest carry over
```

A failed build never touches the live `.duckdb` or parquet files. Freshness
metadata lives in `_mirror.meta` inside the database.

## Development

```bash
uv sync
uv run pytest                        # SQL Server tests auto-skip
WH_TEST_DSN='...' uv run pytest      # include integration tests
```

Design and plans: `docs/plans/`.
