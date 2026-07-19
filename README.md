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

# semantic layer (optional extra): define metrics once, same numbers everywhere
wl = wh.model("waitlist")                     # from semantics/*.yml, bound to the mirror
df = wh.frame(                                # frame() converts anything to your backend
    wl.filter(_.Category == "1")
      .group_by("Specialty")
      .aggregate("patients_waiting", "median_wait_days")
)

# coming later: oracle source, append/upsert push
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

# with extras — excel reader and/or the semantic layer
uv add "warehouse-tools[excel,semantics] @ git+https://github.com/druarnfield/wh"

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

## Semantic layer

Install the `[semantics]` extra (see Install) and drop model YAML into
`semantics/` next to `wh.yaml` (dir configurable via `semantics: dir:`):

```yaml
waitlist:
  table: outpatient_waitlist_current   # bare = main schema; files.x for others
  dimensions:
    Specialty: _.Specialty             # keep the column's exact case, or use
    Category:                          # a genuinely different name — a case-
      expr: _.Category                 # only rename is refused (upstream bug)
      description: "Urgency category"
  measures:
    patients_waiting: _.PatUrnCoded.nunique()
    median_wait_days: _.WaitingTime.median()
```

`wh` wraps no query API: [BSL's fluent
API](https://github.com/boringdata/boring-semantic-layer) is the query
language (`.filter/.group_by/.aggregate/.sql()`); `wh.model()` looks up,
`wh.frame()` converts results (or any frame-ish object) to your backend.
`wh validate` checks models — structure always, full binding when the
mirror file exists.

**Joins:** YAML-declared `joins:` load but querying them is broken upstream
in BSL 0.3.15 (a strict-xfail test watches for the fix). Until then, two
working options: pre-join at the mirror layer (a query-defined table in
`wh.yaml` — best for joins you want centralised), or query-time joins via
the fluent API, which work fully:

```python
wl.join_one(clinics, on=lambda l, r: l.clinic_code == r.code) \
  .group_by("clinics.region").aggregate("patients")   # dims prefixed after a join
```

## Development

```bash
uv sync
uv run pytest                        # SQL Server tests auto-skip
WH_TEST_DSN='...' uv run pytest      # include integration tests
```

Design and plans: `docs/plans/`.
