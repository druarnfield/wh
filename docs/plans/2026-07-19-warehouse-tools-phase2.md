# warehouse-tools Phase 2 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ad-hoc warehouse access from notebooks — `wh.pull()` (query → frame), `wh.land()` (query → DuckDB table, streaming), `wh.register()` (frame → queryable view) — on a narwhals frame boundary with a shared session connection.

**Architecture:** A new `frames.py` handles the boundary: any narwhals-compatible frame / Arrow / DuckDB relation → Arrow, and Arrow → the preferred backend (config `defaults.frames`, else polars > pandas > pyarrow by availability). `Workspace` gains a lazy shared read-write connection (`ws.con`) used by `connect()`, `register()`, `land()`, and `freshness()` — **required** because DuckDB refuses mixed read-only/read-write connections to one file in a single process (empirically verified; the phase-1 `freshness(read_only=True)` breaks under a live notebook connection, fixed here). `MssqlExtractor` grows a `query(sql, batch_size)` method that `pull`/`land` and the mirror seam share.

**Tech Stack:** narwhals (new core dep), polars + pandas (dev-only, for boundary tests), duckdb, pyarrow.

**Conventions:** TDD every task. No Claude mentions in commits. Tests fake the extractor by monkeypatching `wh.sources.mssql.MssqlExtractor` (established pattern in `tests/test_cli.py`).

---

## Task 1: Dependencies

**Step 1:** `uv add narwhals` then `uv add --group dev polars pandas`

**Step 2:** `uv run pytest -q` → 45 passed, 1 skipped (no regressions).

**Step 3:** Commit: `git add -A && git commit -m "chore: add narwhals; polars/pandas for tests"`

---

## Task 2: frames.py — the narwhals boundary

**Files:**
- Create: `src/wh/frames.py`
- Test: `tests/test_frames.py`

**Step 1: Write the failing tests**

```python
import duckdb
import pandas as pd
import polars as pl
import pyarrow as pa
import pytest

from wh.errors import WhError
from wh.frames import default_backend, from_arrow, to_arrow

TABLE = pa.table({"a": [1, 2]})


def test_default_backend_prefers_polars():
    # polars is installed in the dev environment
    assert default_backend() == "polars"


@pytest.mark.parametrize("obj", [
    TABLE,
    pl.DataFrame({"a": [1, 2]}),
    pd.DataFrame({"a": [1, 2]}),
])
def test_to_arrow_roundtrips(obj):
    out = to_arrow(obj)
    assert isinstance(out, pa.Table)
    assert out.column("a").to_pylist() == [1, 2]


def test_to_arrow_duckdb_relation():
    con = duckdb.connect()
    rel = con.sql("SELECT 1 AS a UNION ALL SELECT 2 ORDER BY a")
    assert to_arrow(rel).column("a").to_pylist() == [1, 2]


def test_to_arrow_rejects_junk():
    with pytest.raises(WhError, match="not a supported"):
        to_arrow({"a": [1, 2]})


def test_from_arrow_backends():
    assert isinstance(from_arrow(TABLE, "polars"), pl.DataFrame)
    assert isinstance(from_arrow(TABLE, "pandas"), pd.DataFrame)
    assert from_arrow(TABLE, "pyarrow") is TABLE


def test_from_arrow_unknown_backend():
    with pytest.raises(WhError, match="backend"):
        from_arrow(TABLE, "spark")
```

**Step 2:** `uv run pytest tests/test_frames.py -q` → collection error (module missing).

**Step 3: Implement `src/wh/frames.py`**

```python
"""The frame boundary: narwhals in, Arrow through the core, your backend out.

The library never depends on pandas or polars directly — narwhals accepts
whichever the host project uses, and `from_arrow` hands results back in the
preferred backend (config `defaults.frames`, else best available).
"""

from __future__ import annotations

import importlib.util

import duckdb
import narwhals as nw
import pyarrow as pa

from .errors import WhError

VALID_BACKENDS = ("polars", "pandas", "pyarrow")


def default_backend() -> str:
    for name in ("polars", "pandas"):
        if importlib.util.find_spec(name) is not None:
            return name
    return "pyarrow"


def to_arrow(obj) -> pa.Table:
    """Any supported frame-like object -> pyarrow Table."""
    if isinstance(obj, pa.Table):
        return obj
    if isinstance(obj, pa.RecordBatchReader):
        return obj.read_all()
    if isinstance(obj, duckdb.DuckDBPyRelation):
        return obj.to_arrow_table()
    try:
        return nw.from_native(obj, eager_only=True).to_arrow()
    except TypeError as e:
        raise WhError(
            f"{type(obj).__name__} is not a supported frame type "
            f"(pandas/polars/pyarrow/duckdb relation)"
        ) from e


def from_arrow(table: pa.Table, backend: str):
    if backend == "pyarrow":
        return table
    if backend == "polars":
        import polars as pl

        return pl.from_arrow(table)
    if backend == "pandas":
        return table.to_pandas()
    raise WhError(f"unknown frames backend '{backend}' (use one of {VALID_BACKENDS})")
```

**Step 4:** `uv run pytest tests/test_frames.py -q` → PASS.

**Step 5:** Commit: `feat: narwhals frame boundary (to_arrow/from_arrow/default_backend)`

---

## Task 3: config `defaults.frames`

**Files:**
- Modify: `src/wh/config.py` (Config dataclass + loader)
- Test: `tests/test_config_load.py` (add)

**Step 1: Failing tests**

```python
def test_frames_default_is_none(tmp_path):
    assert load_config(write(tmp_path, VALID)).frames is None


def test_frames_parsed(tmp_path):
    cfg_dict = {**VALID, "defaults": {**VALID["defaults"], "frames": "pandas"}}
    assert load_config(write(tmp_path, cfg_dict)).frames == "pandas"


def test_frames_invalid(tmp_path):
    cfg_dict = {**VALID, "defaults": {**VALID["defaults"], "frames": "spark"}}
    with pytest.raises(ConfigError, match="frames"):
        load_config(write(tmp_path, cfg_dict))
```

**Step 2:** Run → FAIL (`Config` has no `frames`).

**Step 3: Implement** — add to `Config`: `frames: str | None = None`. In `load_config` after `default_mode` validation:

```python
    frames = dfl.get("frames")
    if frames is not None and frames not in VALID_FRAMES:
        raise ConfigError(f"defaults.frames must be one of {sorted(VALID_FRAMES)}")
```

with `VALID_FRAMES = {"polars", "pandas", "pyarrow"}` next to the other constants, and `frames=frames` in the `Config(...)` construction.

**Step 4:** Full suite → PASS. **Step 5:** Commit: `feat: defaults.frames config key`

---

## Task 4: `MssqlExtractor.query()`

Refactor so ad-hoc SQL and the mirror seam share one path. No new unit test (needs a server); the integration test extends in Task 8, and the existing suite guards the seam.

**Files:**
- Modify: `src/wh/sources/mssql.py`

**Step 1: Refactor** — replace `__call__` body:

```python
    def query(self, sql: str, batch_size: int = DEFAULT_BATCH_SIZE):
        """Execute sql, return an Arrow reader (streaming) or table."""
        if self._cursor is not None:
            self._cursor.close()
            self._cursor = None
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql)
        except Exception as e:
            cursor.close()
            raise SourceError(f"query failed: {e}") from e
        self._cursor = cursor
        if hasattr(cursor, "arrow_reader"):
            return cursor.arrow_reader(batch_size=batch_size)
        return cursor.arrow()

    def __call__(self, spec: TableSpec):
        try:
            return self.query(spec.source.sql(), spec.batch_size)
        except SourceError as e:
            raise SourceError(f"extract failed for table '{spec.name}': {e}") from e
```

Import `DEFAULT_BATCH_SIZE` from `..config`.

**Step 2:** Full suite → PASS. **Step 3:** Commit: `refactor: extractor query() shared by mirror seam and ad-hoc pulls`

---

## Task 5: Shared session connection + freshness fix

**Files:**
- Modify: `src/wh/workspace.py`
- Test: `tests/test_workspace.py` (add + adjust)

**Step 1: Failing tests**

```python
def test_shared_connection_is_cached(project):
    ws = Workspace.load(project / "wh.yaml")
    assert ws.con is ws.con
    assert ws.connect() is ws.con          # default connect() = the session con


def test_connect_fresh_is_independent(project):
    ws = Workspace.load(project / "wh.yaml")
    fresh = ws.connect(fresh=True)
    assert fresh is not ws.con
    fresh.close()


def test_freshness_works_while_rw_connection_open(project):
    # regression: read_only + rw on one file in-process is a DuckDB error
    import pyarrow as pa
    ws = Workspace.load(project / "wh.yaml")
    ws.mirror(extract=lambda spec: pa.table({"a": [1]}), log=lambda s: None)
    con = ws.con                            # hold a live rw connection
    assert ws.freshness().num_rows == 1
    assert con.execute("SELECT 1").fetchone() == (1,)


def test_mirror_reopens_shared_connection(project):
    import pyarrow as pa
    ws = Workspace.load(project / "wh.yaml")
    ws.mirror(extract=lambda spec: pa.table({"a": [1]}), log=lambda s: None)
    _ = ws.con
    ws.mirror(extract=lambda spec: pa.table({"a": [2]}), log=lambda s: None)
    assert ws.con.execute('SELECT a FROM "main"."t1"').fetchall() == [(2,)]
```

Also update the two existing tests that close `connect()`'s result (`test_connect_creates_and_queries`, `test_module_level_uses_discovery`): they now use `connect(fresh=True)` — the shared connection must not be closed by tests.

**Step 2:** Run → FAIL (`Workspace` has no attribute `con`).

**Step 3: Implement in `workspace.py`**

```python
    _con: duckdb.DuckDBPyConnection | None = None   # class attr default

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        """The shared session connection (read-write, created lazily).

        One process must not mix read-only and read-write connections to the
        same DuckDB file, so everything in-process shares this one.
        """
        if self._con is not None:
            try:
                self._con.execute("SELECT 1")
            except duckdb.Error:
                self._con = None                    # was closed; reopen
        if self._con is None:
            self._con = duckdb.connect(str(self.config.duckdb_path))
        return self._con

    def connect(self, *, fresh: bool = False, read_only: bool = False):
        """The session connection — hand it to marimo/jupyter.

        fresh=True returns an independent read-write connection you own.
        read_only=True implies fresh; only for OTHER processes' files —
        it will fail if this process already holds a write connection."""
        if read_only:
            return duckdb.connect(str(self.config.duckdb_path), read_only=True)
        if fresh:
            return duckdb.connect(str(self.config.duckdb_path))
        return self.con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None
```

`mirror()`: call `self.close()` first (the swap invalidates the old handle), and `freshness()`: replace the `connect(read_only=True)`/`finally: close` dance with the shared `self.con` (keep the `CatalogException` → `WhError` wrap, drop the `finally`).

`__init__` must also set `self._con = None` (instance attr) in `Workspace.__init__`.

**Step 4:** Full suite → PASS. **Step 5:** Commit: `feat: shared session connection; freshness works beside live connections`

---

## Task 6: `register()`

**Files:**
- Modify: `src/wh/workspace.py`, `src/wh/__init__.py`
- Test: `tests/test_workspace.py` (add)

**Step 1: Failing tests**

```python
def test_register_polars_frame_queryable(project):
    import polars as pl
    ws = Workspace.load(project / "wh.yaml")
    ws.register(pl.DataFrame({"ur": [1, 2, 3]}), "cohort")
    assert ws.con.execute("SELECT count(*) FROM cohort").fetchone() == (3,)


def test_register_returns_none_and_replaces(project):
    import polars as pl
    ws = Workspace.load(project / "wh.yaml")
    ws.register(pl.DataFrame({"x": [1]}), "cohort")
    ws.register(pl.DataFrame({"x": [1, 2]}), "cohort")   # re-register wins
    assert ws.con.execute("SELECT count(*) FROM cohort").fetchone() == (2,)
```

**Step 2:** Run → FAIL. **Step 3: Implement**

```python
    def register(self, frame, name: str) -> None:
        """Make any dataframe queryable (as `name`) on the session connection."""
        from .frames import to_arrow

        self.con.register(name, to_arrow(frame))
```

Module-level in `__init__.py`: `def register(frame, name): return workspace().register(frame, name)` (+ `__all__`).

**Step 4:** PASS. **Step 5:** Commit: `feat: register() — any frame queryable beside mirror tables`

---

## Task 7: `pull()` and `land()`

**Files:**
- Modify: `src/wh/workspace.py`, `src/wh/__init__.py`
- Test: `tests/test_pull_land.py`

**Step 1: Failing tests**

```python
import duckdb
import polars as pl
import pyarrow as pa
import pandas as pd
import pytest

import wh
from wh.errors import ConfigError, WhError
from wh.workspace import Workspace


@pytest.fixture
def fake_mssql(monkeypatch):
    """Patch MssqlExtractor with a fake serving canned queries."""
    import wh.sources.mssql as mssql_mod

    class FakeExtractor:
        def __init__(self, source):
            self.source = source

        def query(self, sql, batch_size=100_000):
            return pa.table({"n": [1, 2, 3], "q": [sql] * 3})

        def close(self):
            pass

    monkeypatch.setattr(mssql_mod, "MssqlExtractor", FakeExtractor)
    return FakeExtractor


def test_pull_returns_default_backend(project, fake_mssql):
    ws = Workspace.load(project / "wh.yaml")
    df = ws.pull("SELECT 1")
    assert isinstance(df, pl.DataFrame)      # polars installed -> default
    assert df["n"].to_list() == [1, 2, 3]


def test_pull_backend_override(project, fake_mssql):
    ws = Workspace.load(project / "wh.yaml")
    assert isinstance(ws.pull("SELECT 1", backend="pandas"), pd.DataFrame)


def test_pull_config_frames_wins(project, fake_mssql):
    (project / "wh.yaml").write_text(
        (project / "wh.yaml").read_text().replace(
            "destination:", "defaults:\n  frames: pyarrow\ndestination:"
        )
    )
    ws = Workspace.load(project / "wh.yaml")
    assert isinstance(ws.pull("SELECT 1"), pa.Table)


def test_pull_unknown_source(project, fake_mssql):
    ws = Workspace.load(project / "wh.yaml")
    with pytest.raises(ConfigError, match="nope.*warehouse"):
        ws.pull("SELECT 1", source="nope")


def test_land_creates_table(project, fake_mssql):
    ws = Workspace.load(project / "wh.yaml")
    rows = ws.land("SELECT 1", table="scratch.raw")
    assert rows == 3
    assert ws.con.execute('SELECT count(*) FROM "scratch"."raw"').fetchone() == (3,)


def test_land_default_schema_and_replace(project, fake_mssql):
    ws = Workspace.load(project / "wh.yaml")
    ws.land("SELECT 1", table="raw")
    ws.land("SELECT 2", table="raw")         # re-land replaces
    assert ws.con.execute('SELECT count(*) FROM "main"."raw"').fetchone() == (3,)


def test_land_bad_identifier(project, fake_mssql):
    ws = Workspace.load(project / "wh.yaml")
    with pytest.raises(WhError, match="table"):
        ws.land("SELECT 1", table="a.b.c")


def test_module_level_pull(project, fake_mssql, monkeypatch):
    monkeypatch.chdir(project)
    monkeypatch.setattr(wh, "_default", None)
    assert wh.pull("SELECT 1")["n"].to_list() == [1, 2, 3]
```

**Step 2:** Run → FAIL. **Step 3: Implement in `workspace.py`**

```python
    def _source(self, name: str | None):
        cfg = self.config
        key = name or cfg.default_source
        if key not in cfg.sources:
            raise ConfigError(
                f"unknown source '{key}' (configured: {', '.join(cfg.sources)})"
            )
        return cfg.sources[key]

    def pull(self, sql: str, *, source: str | None = None, backend: str | None = None):
        """Run sql against a warehouse source, return a dataframe."""
        from .frames import default_backend, from_arrow, to_arrow
        from .sources.mssql import MssqlExtractor

        extractor = MssqlExtractor(self._source(source))
        try:
            table = to_arrow(extractor.query(sql))
        finally:
            extractor.close()
        return from_arrow(table, backend or self.config.frames or default_backend())

    def land(self, sql: str, table: str, *, source: str | None = None) -> int:
        """Stream sql results straight into the local DuckDB (constant memory).

        Re-landing the same table replaces it. Returns the row count."""
        from .sources.mssql import MssqlExtractor

        parts = table.split(".")
        if len(parts) == 1:
            schema, name = "main", parts[0]
        elif len(parts) == 2:
            schema, name = parts
        else:
            raise WhError(f"table must be 'name' or 'schema.name', got '{table}'")
        qualified = f'"{schema}"."{name}"'

        con = self.con
        extractor = MssqlExtractor(self._source(source))
        try:
            con.register("_wh_land_src", extractor.query(sql))
            try:
                con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                con.execute(
                    f"CREATE OR REPLACE TABLE {qualified} AS SELECT * FROM _wh_land_src"
                )
            finally:
                con.unregister("_wh_land_src")
        finally:
            extractor.close()
        (count,) = con.execute(f"SELECT count(*) FROM {qualified}").fetchone()
        return count
```

Imports at top of workspace.py: `from .errors import ConfigError, WhError`. Module-level wrappers in `__init__.py`:

```python
def pull(sql, **kwargs):
    return workspace().pull(sql, **kwargs)


def land(sql, table, **kwargs):
    return workspace().land(sql, table, **kwargs)
```

(+ `__all__` entries for `pull`, `land`, `register`.)

**Step 4:** Full suite → PASS. **Step 5:** Commit: `feat: pull() and land() — ad-hoc warehouse queries to frames or DuckDB`

---

## Task 8: Integration tests + docs

**Files:**
- Modify: `tests/test_mssql_integration.py`, `README.md`, `CLAUDE.md`

**Step 1: Add integration tests** (same env-gated file):

```python
def test_pull_and_land_roundtrip(tmp_path, monkeypatch):
    import yaml
    import wh

    cfg = {
        "sources": {"warehouse": {"driver": "mssql", "dsn_env": "WH_TEST_DSN"}},
        "destination": {"duckdb_path": "./t.duckdb"},
        "tables": [{"name": "x", "source": {"query": "SELECT 1 AS a"}}],
    }
    (tmp_path / "wh.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    ws = wh.workspace(tmp_path / "wh.yaml")

    df = ws.pull("SELECT 2 + 2 AS four")
    assert df["four"].to_list() == [4]

    assert ws.land("SELECT 1 AS n UNION ALL SELECT 2", table="scratch.nums") == 2
    assert ws.con.execute('SELECT sum(n) FROM "scratch"."nums"').fetchone() == (3,)
    ws.close()
```

**Step 2:** `WH_TEST_DSN='...' uv run pytest tests/test_mssql_integration.py -q` → 2 passed. Without the env var → skipped.

**Step 3:** Update README (move pull/land/register out of "coming in later phases"; add one-line examples) and CLAUDE.md (phase 2 COMPLETE, shared-connection rule: *never* open read-only alongside the session con in-process; `connect(fresh=True)` for independent connections).

**Step 4:** Full suite + acceptance: `uv run pytest -q`, then against the dev server: pull, land, register smoke via `uv run python -c ...`.

**Step 5:** Commit: `feat: phase 2 — pull/land/register with narwhals boundary (docs + integration)`

---

## Done — phase 2 acceptance

- `wh.pull("SELECT ...")` → polars frame (or configured backend) in a notebook
- `wh.land(sql, table="scratch.x")` streams into the mirror db; re-land replaces
- `wh.register(df, "cohort")` joins against mirror tables on the session con
- `wh.freshness()` works while the session connection is open (phase-1 bug fixed)
- Suite green without a server; integration green with `WH_TEST_DSN`
