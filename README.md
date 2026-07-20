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

wh.push(results, "Sandbox.dbo.analysis")               # publish results back
wh.push(results, "Sandbox.dbo.analysis", if_exists="replace")   # republish

raw = wh.read_excel("messy.xlsx")        # finds the real header under title rows
df = wh.clean(raw,                       # composable cleaners, polars or pandas
    wh.clean.snake_names,                # "Referral Date " -> referral_date
    wh.clean.drop_empty,                 # all-null rows and columns
    wh.clean.strip_strings,
    wh.clean.parse_dates("referral_date", format="%d/%m/%Y"),
    wh.clean.numeric("amount"),          # "$1,250.00", "-" -> 1250.0, null
)
df = wh.read_csv("easy.csv")             # DuckDB's sniffing reader
wh.read_excel("messy.xlsx", land="files.raw")   # or straight into the .duckdb

# coming later: metrics layer (in development), oracle source, append/upsert push
```

`read_excel` takes `sheet=` (name or index), `header=` (`"auto"` default, an
explicit row index, `(top, bottom)` for multi-row headers with merged cells,
or `None`), and `skip_rows=`. Mixed-type columns degrade to strings rather
than erroring — `wh.clean.numeric` sorts them out. Both readers work without
a `wh.yaml` unless you use `land=`. Excel needs the `[excel]` extra (see
Install).

`push()` only writes to schemas listed under `push.allow` in `wh.yaml`
(`PushRefused` otherwise), takes three-part names (`Database.schema.table`),
and runs create + insert in one transaction. The Arrow→SQL Server type map
lives in `src/wh/push.py`'s docstring.

`pull()` returns polars if installed (else pandas, else pyarrow); set
`defaults.frames` in `wh.yaml` to pin it. `wh.connect()` returns a shared
session connection — DuckDB forbids mixing read-only and read-write
connections to one file in a process, so don't open your own read-only ones;
`wh.connect(fresh=True)` gives an independent read-write connection.

## Install

Into an analysis project:

```bash
# core (mirror, pull/land/register, push, csv)
uv add "warehouse-tools @ git+https://github.com/druarnfield/wh"

# with the excel reader extra
uv add "warehouse-tools[excel] @ git+https://github.com/druarnfield/wh"

# working on wh itself
uv add --editable /path/to/wh
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

push:
  allow: [Sandbox.dbo]                   # schemas push() may write to

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

## Metrics layer

In development — a home-grown metrics layer (versioned measure definitions
over fact tables, free slicing that structurally cannot alter a measure's
meaning, full provenance on every number). Design:
`docs/plans/2026-07-20-metrics-design.md`. The earlier BSL-based semantic
layer was removed.

## Development

```bash
uv sync
uv run pytest                        # SQL Server tests auto-skip
WH_TEST_DSN='...' uv run pytest      # include integration tests
```

Design and plans: `docs/plans/`.
