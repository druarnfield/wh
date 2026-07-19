# warehouse-tools Phase 3 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** `wh.push(frame, "Db.schema.table")` — publish analysis results back to SQL Server, gated by a config allowlist, with create/replace semantics and a documented Arrow→SQL Server type mapping.

**Architecture:** New `src/wh/push.py` holds pure, unit-testable pieces (identifier parsing/quoting with `[...]` escaping, allowlist check, Arrow field → SQL Server type mapping, CREATE/INSERT SQL builders) plus an orchestrator `push_arrow(conn, ...)` that takes any DB-API connection — unit tests drive it with a fake connection recording SQL; only the integration test touches the real server. `Workspace.push()` converts any frame/relation via `frames.to_arrow`, checks the allowlist, opens the mssql connection, and wraps the whole thing in commit/rollback. Target names are **three-part** (`Database.schema.table`) to match allowlist entries (`Database.schema`); comparisons are case-insensitive (SQL Server default collation).

**Tech Stack:** mssql-python (DB-API cursor + executemany), pyarrow, existing frames boundary.

**Conventions:** TDD. No Claude mentions in commits. `set -o pipefail` when chaining pytest into git. Notebook-friendly `WhError` messages.

---

## Task 1: Config `push.allow`

**Files:**
- Modify: `src/wh/config.py`
- Test: `tests/test_config_load.py` (add)

**Step 1: Failing tests**

```python
def test_push_allow_default_empty(tmp_path):
    assert load_config(write(tmp_path, VALID)).push_allow == []


def test_push_allow_parsed(tmp_path):
    cfg_dict = {**VALID, "push": {"allow": ["Sandbox.dbo", "Sandbox.analysis"]}}
    assert load_config(write(tmp_path, cfg_dict)).push_allow == [
        "Sandbox.dbo", "Sandbox.analysis"
    ]


def test_push_allow_entry_must_be_two_part(tmp_path):
    cfg_dict = {**VALID, "push": {"allow": ["Sandbox"]}}
    with pytest.raises(ConfigError, match="Database.schema"):
        load_config(write(tmp_path, cfg_dict))
```

**Step 2:** Run → FAIL. **Step 3: Implement** — `Config` gains `push_allow: list[str] = field(default_factory=list)`; in `load_config` before the `tables` loop:

```python
    push_allow = list((raw.get("push") or {}).get("allow") or [])
    for entry in push_allow:
        if len(str(entry).split(".")) != 2:
            raise ConfigError(
                f"push.allow entry '{entry}' must be 'Database.schema'"
            )
```

pass `push_allow=push_allow` to `Config(...)`.

**Step 4:** Full suite → PASS. **Step 5:** Commit: `feat: push.allow config allowlist`

---

## Task 2: push.py pure helpers

**Files:**
- Create: `src/wh/push.py`
- Test: `tests/test_push.py`

**Step 1: Failing tests**

```python
import pyarrow as pa
import pytest

from wh.errors import PushRefused, WhError
from wh.push import check_allowed, parse_target, sql_type


def test_parse_target():
    assert parse_target("Sandbox.dbo.results") == ("Sandbox", "dbo", "results")


def test_parse_target_requires_three_parts():
    for bad in ("results", "dbo.results", "a.b.c.d", "a..c"):
        with pytest.raises(WhError, match="Database.schema.table"):
            parse_target(bad)


def test_check_allowed_case_insensitive():
    check_allowed("SANDBOX", "DBO", ["Sandbox.dbo"])   # no raise


def test_check_allowed_refuses_and_names_allowlist():
    with pytest.raises(PushRefused, match=r"Sandbox\.dbo"):
        check_allowed("Prod", "dbo", ["Sandbox.dbo"])


def test_check_allowed_empty_allowlist_message():
    with pytest.raises(PushRefused, match="push.allow"):
        check_allowed("Sandbox", "dbo", [])


@pytest.mark.parametrize("arrow_type,expected", [
    (pa.int8(), "SMALLINT"),
    (pa.int16(), "SMALLINT"),
    (pa.int32(), "INT"),
    (pa.int64(), "BIGINT"),
    (pa.uint32(), "BIGINT"),
    (pa.float32(), "REAL"),
    (pa.float64(), "FLOAT"),
    (pa.bool_(), "BIT"),
    (pa.string(), "NVARCHAR(MAX)"),
    (pa.large_string(), "NVARCHAR(MAX)"),
    (pa.date32(), "DATE"),
    (pa.timestamp("us"), "DATETIME2"),
    (pa.time64("us"), "TIME"),
    (pa.decimal128(18, 4), "DECIMAL(18,4)"),
    (pa.binary(), "VARBINARY(MAX)"),
])
def test_sql_type_mapping(arrow_type, expected):
    assert sql_type(pa.field("c", arrow_type)) == expected


def test_sql_type_unsupported():
    with pytest.raises(WhError, match="list"):
        sql_type(pa.field("c", pa.list_(pa.int64())))
```

**Step 2:** Run → collection error. **Step 3: Implement `src/wh/push.py`**

```python
"""Writeback to SQL Server: allowlist gate, type mapping, SQL builders.

The Arrow -> SQL Server type mapping (the contract for pushed tables):

    int8/int16      SMALLINT        date32/date64   DATE
    int32/uint16    INT             timestamp[*]    DATETIME2
    int64/uint32+   BIGINT          time32/time64   TIME
    float32         REAL            decimal(p,s)    DECIMAL(p,s)
    float64         FLOAT           binary          VARBINARY(MAX)
    bool            BIT             string          NVARCHAR(MAX)

Anything else (lists, structs, ...) is refused with a WhError.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.types as pat

from .errors import PushRefused, WhError


def parse_target(table: str) -> tuple[str, str, str]:
    parts = table.split(".")
    if len(parts) != 3 or not all(parts):
        raise WhError(
            f"push target must be 'Database.schema.table', got '{table}'"
        )
    return tuple(parts)


def check_allowed(database: str, schema: str, allow: list[str]) -> None:
    if not allow:
        raise PushRefused(
            "no push.allow entries in wh.yaml — add the schemas you may "
            "write to, e.g.  push:\n  allow: [Sandbox.dbo]"
        )
    key = f"{database}.{schema}".lower()
    if key not in {a.lower() for a in allow}:
        raise PushRefused(
            f"'{database}.{schema}' is not in push.allow "
            f"(allowed: {', '.join(allow)})"
        )


def quote(ident: str) -> str:
    return "[" + ident.replace("]", "]]") + "]"


def sql_type(field: pa.Field) -> str:
    t = field.type
    if pat.is_int8(t) or pat.is_int16(t):
        return "SMALLINT"
    if pat.is_int32(t) or pat.is_uint16(t) or pat.is_uint8(t):
        return "INT"
    if pat.is_int64(t) or pat.is_uint32(t) or pat.is_uint64(t):
        return "BIGINT"
    if pat.is_float32(t):
        return "REAL"
    if pat.is_float64(t):
        return "FLOAT"
    if pat.is_boolean(t):
        return "BIT"
    if pat.is_string(t) or pat.is_large_string(t):
        return "NVARCHAR(MAX)"
    if pat.is_date(t):
        return "DATE"
    if pat.is_timestamp(t):
        return "DATETIME2"
    if pat.is_time(t):
        return "TIME"
    if pat.is_decimal(t):
        return f"DECIMAL({t.precision},{t.scale})"
    if pat.is_binary(t) or pat.is_large_binary(t):
        return "VARBINARY(MAX)"
    raise WhError(
        f"column '{field.name}': cannot push arrow type {t} to SQL Server"
    )
```

(`tuple(parts)` — add `# type: ignore` not needed; it returns a 3-tuple at runtime.)

**Step 4:** PASS. **Step 5:** Commit: `feat: push helpers — target parsing, allowlist, arrow->mssql type map`

---

## Task 3: push orchestrator (fake DB-API connection)

**Files:**
- Modify: `src/wh/push.py`
- Test: `tests/test_push.py` (add)

**Step 1: Failing tests**

```python
class FakeCursor:
    def __init__(self, table_exists):
        self.table_exists = table_exists
        self.executed: list[str] = []
        self.many: list[tuple[str, list]] = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        return self

    def fetchone(self):
        return (1 if self.table_exists else 0,)

    def executemany(self, sql, rows):
        self.many.append((sql, list(rows)))

    def close(self):
        pass


class FakeConn:
    def __init__(self, table_exists=False):
        self.cur = FakeCursor(table_exists)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


TABLE = pa.table({"n": [1, 2], "s": ["a", None]})


def test_push_arrow_creates_and_inserts():
    from wh.push import push_arrow

    conn = FakeConn(table_exists=False)
    assert push_arrow(conn, "Sandbox", "dbo", "res", TABLE, if_exists="fail") == 2
    create = next(s for s in conn.cur.executed if s.startswith("CREATE TABLE"))
    assert create == (
        "CREATE TABLE [Sandbox].[dbo].[res] ([n] BIGINT, [s] NVARCHAR(MAX))"
    )
    insert_sql, rows = conn.cur.many[0]
    assert insert_sql == "INSERT INTO [Sandbox].[dbo].[res] ([n], [s]) VALUES (?, ?)"
    assert rows == [(1, "a"), (2, None)]
    assert conn.commits == 1


def test_push_arrow_fail_when_exists():
    from wh.push import push_arrow

    conn = FakeConn(table_exists=True)
    with pytest.raises(WhError, match="if_exists='replace'"):
        push_arrow(conn, "Sandbox", "dbo", "res", TABLE, if_exists="fail")
    assert conn.rollbacks == 1


def test_push_arrow_replace_drops_first():
    from wh.push import push_arrow

    conn = FakeConn(table_exists=True)
    push_arrow(conn, "Sandbox", "dbo", "res", TABLE, if_exists="replace")
    assert "DROP TABLE [Sandbox].[dbo].[res]" in conn.cur.executed
    assert conn.commits == 1


def test_push_arrow_bad_if_exists():
    from wh.push import push_arrow

    with pytest.raises(WhError, match="if_exists"):
        push_arrow(FakeConn(), "Sandbox", "dbo", "res", TABLE, if_exists="append")


def test_push_arrow_empty_table_creates_no_insert():
    from wh.push import push_arrow

    conn = FakeConn(table_exists=False)
    empty = TABLE.slice(0, 0)
    assert push_arrow(conn, "Sandbox", "dbo", "res", empty, if_exists="fail") == 0
    assert conn.cur.many == []
    assert conn.commits == 1


def test_push_arrow_rolls_back_on_error():
    from wh.push import push_arrow

    conn = FakeConn(table_exists=False)

    def explode(sql, rows):
        raise RuntimeError("bulk load failed")

    conn.cur.executemany = explode
    with pytest.raises(RuntimeError):
        push_arrow(conn, "Sandbox", "dbo", "res", TABLE, if_exists="fail")
    assert conn.rollbacks == 1
    assert conn.commits == 0
```

**Step 2:** Run → FAIL. **Step 3: Implement (append to push.py)**

```python
_EXISTS_SQL = (
    "SELECT count(*) FROM {db}.INFORMATION_SCHEMA.TABLES "
    "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?"
)


def push_arrow(
    conn,
    database: str,
    schema: str,
    name: str,
    table: pa.Table,
    *,
    if_exists: str = "fail",
    batch_size: int = 5_000,
) -> int:
    """Create (or replace) [database].[schema].[name] from an Arrow table.

    Runs entirely in one transaction on `conn` (any DB-API connection):
    commit on success, rollback on any failure."""
    if if_exists not in ("fail", "replace"):
        raise WhError(f"if_exists must be 'fail' or 'replace', got '{if_exists}'")

    qualified = f"{quote(database)}.{quote(schema)}.{quote(name)}"
    columns = ", ".join(f"{quote(f.name)} {sql_type(f)}" for f in table.schema)
    cursor = conn.cursor()
    try:
        cursor.execute(_EXISTS_SQL.format(db=quote(database)), [schema, name])
        (exists,) = cursor.fetchone()
        if exists:
            if if_exists == "fail":
                raise WhError(
                    f"{database}.{schema}.{name} already exists — "
                    f"pass if_exists='replace' to overwrite"
                )
            cursor.execute(f"DROP TABLE {qualified}")
        cursor.execute(f"CREATE TABLE {qualified} ({columns})")

        if table.num_rows:
            col_list = ", ".join(quote(f.name) for f in table.schema)
            placeholders = ", ".join("?" * table.num_columns)
            insert = f"INSERT INTO {qualified} ({col_list}) VALUES ({placeholders})"
            cols = [c.to_pylist() for c in table.columns]
            rows = list(zip(*cols))
            for i in range(0, len(rows), batch_size):
                cursor.executemany(insert, rows[i : i + batch_size])
        conn.commit()
        return table.num_rows
    except BaseException:
        conn.rollback()
        raise
    finally:
        cursor.close()
```

**Step 4:** PASS. **Step 5:** Commit: `feat: transactional push orchestrator over any DB-API connection`

---

## Task 4: `Workspace.push()` + module wrapper

**Files:**
- Modify: `src/wh/workspace.py`, `src/wh/__init__.py`, `src/wh/sources/mssql.py`
- Test: `tests/test_push.py` (add)

**Step 1: Failing tests**

```python
def test_workspace_push_checks_allowlist_before_connecting(project, monkeypatch):
    import polars as pl
    import wh.sources.mssql as mssql_mod
    from wh.workspace import Workspace

    def no_connect(source):
        raise AssertionError("must refuse before opening a connection")

    monkeypatch.setattr(mssql_mod, "open_connection", no_connect)
    ws = Workspace.load(project / "wh.yaml")   # project fixture has no push.allow
    with pytest.raises(PushRefused, match="push.allow"):
        ws.push(pl.DataFrame({"a": [1]}), "Sandbox.dbo.res")


def test_workspace_push_happy_path(project, monkeypatch):
    import polars as pl
    import yaml
    import wh.sources.mssql as mssql_mod
    from wh.workspace import Workspace

    cfg = yaml.safe_load((project / "wh.yaml").read_text())
    cfg["push"] = {"allow": ["Sandbox.dbo"]}
    (project / "wh.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))

    conn = FakeConn(table_exists=False)
    monkeypatch.setattr(mssql_mod, "open_connection", lambda source: conn)
    ws = Workspace.load(project / "wh.yaml")
    assert ws.push(pl.DataFrame({"a": [1, 2, 3]}), "Sandbox.dbo.res") == 3
    assert conn.commits == 1
```

**Step 2:** Run → FAIL. **Step 3: Implement**

In `sources/mssql.py`, extract the connection-opening from `MssqlExtractor.__init__` into a module function both share (push needs a write connection without extractor machinery):

```python
def open_connection(source: Source):
    try:
        import mssql_python
    except ImportError as e:
        raise SourceError("mssql-python is not installed (uv add mssql-python)") from e
    try:
        return mssql_python.connect(source.connection_string())
    except SourceError:
        raise
    except Exception as e:
        raise SourceError(f"could not connect to source '{source.name}': {e}") from e
```

(`MssqlExtractor.__init__` becomes `self._conn = open_connection(source); self._cursor = None`.)

In `workspace.py`:

```python
    def push(
        self,
        frame,
        table: str,
        *,
        source: str | None = None,
        if_exists: str = "fail",
    ) -> int:
        """Publish a dataframe/relation to SQL Server. Allowlist-gated.

        `table` is 'Database.schema.table'. if_exists: 'fail' | 'replace'."""
        from .frames import to_arrow
        from .push import check_allowed, parse_target, push_arrow
        from .sources.mssql import open_connection

        database, schema, name = parse_target(table)
        check_allowed(database, schema, self.config.push_allow)
        arrow = to_arrow(frame)
        conn = open_connection(self._source(source))
        try:
            return push_arrow(
                conn, database, schema, name, arrow, if_exists=if_exists
            )
        finally:
            conn.close()
```

Module wrapper in `__init__.py` (+ `__all__` already lists `push`... it does NOT yet — add it):

```python
def push(frame, table, **kwargs):
    return workspace().push(frame, table, **kwargs)
```

**Step 4:** Full suite → PASS. **Step 5:** Commit: `feat: Workspace.push() and wh.push() — allowlist-gated writeback`

---

## Task 5: Integration test, config, docs, acceptance

**Files:**
- Modify: `tests/test_mssql_integration.py`, `wh.yaml`, `README.md`, `CLAUDE.md`

**Step 1: Integration test** (env-gated file; uses the dev database itself as the sandbox):

```python
def test_push_roundtrip(tmp_path):
    import polars as pl
    import yaml
    import wh

    cfg = {
        "sources": {"warehouse": {"driver": "mssql", "dsn_env": "WH_TEST_DSN"}},
        "destination": {"duckdb_path": "./t.duckdb"},
        "push": {"allow": ["ExecReporting.dbo"]},
        "tables": [{"name": "x", "source": {"query": "SELECT 1 AS a"}}],
    }
    (tmp_path / "wh.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    ws = wh.workspace(tmp_path / "wh.yaml")

    df = pl.DataFrame({"n": [1, 2, 3], "s": ["x", "y", None]})
    target = "ExecReporting.dbo.wh_push_test"
    try:
        assert ws.push(df, target) == 3
        assert ws.push(df, target, if_exists="replace") == 3
        with pytest.raises(wh.WhError, match="replace"):
            ws.push(df, target)
        back = ws.pull("SELECT n, s FROM [ExecReporting].[dbo].[wh_push_test] ORDER BY n")
        assert back["n"].to_list() == [1, 2, 3]
        assert back["s"].to_list() == ["x", "y", None]
        with pytest.raises(wh.PushRefused):
            ws.push(df, "ExecReporting.other_schema.t")
    finally:
        conn = None
        try:
            from wh.sources.mssql import open_connection
            conn = open_connection(ws.config.sources["warehouse"])
            cur = conn.cursor()
            cur.execute("DROP TABLE IF EXISTS [ExecReporting].[dbo].[wh_push_test]")
            conn.commit()
        finally:
            if conn is not None:
                conn.close()
        ws.close()
```

**Step 2:** Run with `WH_TEST_DSN` → 3 passed. Without → skipped.

**Step 3:** `wh.yaml`: add

```yaml
push:
  allow:
    - ExecReporting.dbo # dev sandbox; point at your real sandbox schema(s)
```

**Step 4:** README: move `wh.push` out of "coming in later phases" (leaving `read_excel`), add the allowlist snippet and if_exists example. CLAUDE.md: phase 3 COMPLETE, gotchas (type map lives in push.py docstring; push is 3-part names only).

**Step 5:** Full suite + live acceptance (`uv run python` pushing a real frame, verified via pull). Commit: `feat: phase 3 — push() writeback with allowlist (docs + integration)`

---

## Done — phase 3 acceptance

- `wh.push(df, "Db.schema.table")` creates; `if_exists="replace"` republishes; second create fails friendly
- Non-allowlisted schema → `PushRefused` naming the allowlist, before any connection is opened
- Pushed data round-trips through `wh.pull` including NULLs
- Type map documented in `push.py` docstring; unsupported types refused clearly
- Suite green serverless; integration green with `WH_TEST_DSN`
