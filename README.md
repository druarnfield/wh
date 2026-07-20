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

# metrics layer: governed numbers from semantics/*.yml (see Metrics layer below)
s = wh.slice("waitlist", measures=["patients_waiting"],
             by=["facility.region"], context=wh.context(time=wh.last(12, "month")))
s.frame()

# coming later: compare=/provenance, oracle source, append/upsert push
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

Versioned measure definitions over fact tables, with slicing that
structurally cannot alter a measure's meaning: a measure's intrinsic
`where` compiles into per-measure `FILTER` clauses, your filter context
compiles into the outer `WHERE`, and snapshot models always evaluate at
one global as-at moment per period. Design (worth reading):
`docs/plans/2026-07-20-metrics-design.md`.

Drop model YAML into `semantics/` next to `wh.yaml`:

```yaml
dimensions:                 # shared/conformed — defined once
  facility:
    table: main.clinic_dim
    key_column: clinic_code
    attributes: {clinic: clinic_name, region: region}
    hierarchy: [clinic, region]

waitlist:
  fact: main.waitlist
  time: {column: snapshot_date, cadence: weekly}
  snapshot: true            # stock: last-snapshot row selection, always
  dimensions:
    facility: clinic_code   # shared dim: fact-side key only
    urgency: urgency_category
  measures:
    patients_waiting:
      expr: count(DISTINCT ur)
      description: "Distinct patients on the list at snapshot"
    long_waiters:
      expr: count(*)
      where: wait_days > 365          # intrinsic — part of the definition
      description: "Waiting beyond 365 days at snapshot"
```

```python
ctx = wh.context(facility__region="North", time=("2025-07-01", "2026-06-30"))
s = wh.slice("waitlist", measures=["patients_waiting", "long_waiters"],
             by=["facility.region"], context=ctx, grain="month")
s.frame()          # your preferred backend
s.sql              # the exact generated SQL — two lanes, visibly separate
s.view("summary")  # register for marimo SQL cells
```

Contexts are values: `ctx.with_(...)`, `ctx.without(...)`, `ctx | other`,
`wh.not_(x)`, `wh.last(12, "month")` (anchored to mirror data, not wall
clock), widgets accepted directly (`.value` read at slice time). Fiscal
grains (`fy`, `fy_quarter`, July start etc.) come from
`semantics: fiscal_year_start:` in `wh.yaml`. `wh validate` checks the
models — structure always, full bind checks (dim-key uniqueness, orphan
keys, intrinsic-where rules) when the mirror file exists. Coming next:
`compare=` (yoy/fytd), `complete_periods`, suppression, provenance.

## Development

```bash
uv sync
uv run pytest                        # SQL Server tests auto-skip
WH_TEST_DSN='...' uv run pytest      # include integration tests
```

Design and plans: `docs/plans/`.
