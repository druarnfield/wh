# warehouse-tools Phase 1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Turn the mirror.py POC into the installable `wh` package: config with named sources, `Workspace`/`wh.connect()`, and the mirror refactor with `_mirror.meta` and carry-over `--only`.

**Architecture:** src-layout package `wh` (project name `warehouse-tools`). Config (`wh.yaml`) is discovered by walking up from cwd; relative paths resolve against the config file's directory. `Workspace` is the hub; module-level `wh.connect()/mirror()/freshness()` delegate to a lazy default workspace. The mirror keeps POC semantics (staging build → atomic swap) but extraction goes through a seam — `extract(spec) -> Arrow` — so all mirror logic is testable with fake Arrow data; the real `MssqlExtractor` implements the same seam.

**Tech Stack:** Python ≥3.13, uv (uv_build backend), duckdb, pyarrow, mssql-python, pyyaml, pytest.

**Design doc:** `docs/plans/2026-07-19-warehouse-tools-design.md` — read it first.

**Conventions:** TDD every task (test → fail → implement → pass → commit). Do not mention Claude in commit messages. No worktree — work directly on `main`.

---

## Task 1: Package skeleton

No TDD here (pure infrastructure), but verify each step.

**Files:**
- Modify: `pyproject.toml`
- Create: `src/wh/__init__.py`, `tests/__init__.py`
- Delete: `main.py`

**Step 1: Rewrite `pyproject.toml`**

```toml
[project]
name = "warehouse-tools"
version = "0.1.0"
description = "Helpers for mirroring warehouse data into DuckDB and pushing results back"
readme = "README.md"
requires-python = ">=3.13"
dependencies = [
    "duckdb>=1.5.4",
    "mssql-python>=1.11.0",
    "pyarrow>=25.0.0",
    "pyyaml>=6.0.3",
]

[project.scripts]
wh = "wh.cli:main"

[build-system]
requires = ["uv_build>=0.9"]
build-backend = "uv_build"

[tool.uv.build-backend]
module-name = "wh"

[dependency-groups]
dev = ["pytest>=8"]
```

(`module-name = "wh"` is required: uv_build otherwise expects `src/warehouse_tools`.)

**Step 2: Create the package and test dirs, remove the uv-init stub**

```bash
mkdir -p src/wh tests
touch src/wh/__init__.py tests/__init__.py
rm main.py
```

**Step 3: Sync and verify**

Run: `uv sync`
Expected: resolves and installs `warehouse-tools` as editable + pytest. (The `wh` script will fail until Task 12 creates `cli.py` — that's fine, don't run it yet.)

Run: `uv run pytest`
Expected: `no tests ran` (exit code 5 is fine).

**Step 4: Commit**

```bash
git add -A
git commit -m "feat: package skeleton for warehouse-tools (src layout, wh module)"
```

---

## Task 2: Errors module

**Files:**
- Create: `src/wh/errors.py`
- Test: `tests/test_errors.py`

**Step 1: Write the failing test**

```python
from wh.errors import WhError, ConfigError, SourceError, PushRefused, SchemaMismatch


def test_all_errors_inherit_wherror():
    for exc in (ConfigError, SourceError, PushRefused, SchemaMismatch):
        assert issubclass(exc, WhError)
    assert issubclass(WhError, Exception)
```

**Step 2: Run it** — `uv run pytest tests/test_errors.py -v` → FAIL (`ModuleNotFoundError`).

**Step 3: Implement `src/wh/errors.py`**

```python
"""Exception family for wh. All library errors inherit WhError.

Messages should be notebook-friendly: one clear sentence plus the fix,
never a bare driver stack trace.
"""


class WhError(Exception):
    pass


class ConfigError(WhError):
    pass


class SourceError(WhError):
    pass


class PushRefused(WhError):
    pass


class SchemaMismatch(WhError):
    pass
```

**Step 4: Run it** — `uv run pytest tests/test_errors.py -v` → PASS.

**Step 5: Commit** — `git add -A && git commit -m "feat: error hierarchy"`

---

## Task 3: Named sources + connection string assembly

**Files:**
- Create: `src/wh/config.py` (sources part only)
- Test: `tests/test_config_sources.py`

**Step 1: Write the failing tests**

```python
import pytest

from wh.config import Auth, Source
from wh.errors import ConfigError


def make_source(**kw) -> Source:
    base = dict(
        name="warehouse",
        driver="mssql",
        server="localhost,1433",
        database="ExecReporting",
    )
    base.update(kw)
    return Source(**base)


def test_connection_string_user_password(monkeypatch):
    monkeypatch.setenv("WH_PWD", "s3cret")
    src = make_source(
        encrypt=False,
        trust_server_certificate=True,
        auth=Auth(user="sa", password_env="WH_PWD"),
    )
    assert src.connection_string() == (
        "Server=localhost,1433;Database=ExecReporting;"
        "UID=sa;PWD=s3cret;Encrypt=no;TrustServerCertificate=yes;"
    )


def test_connection_string_trusted():
    src = make_source(auth=Auth(trusted=True))
    assert src.connection_string() == (
        "Server=localhost,1433;Database=ExecReporting;"
        "Trusted_Connection=yes;Encrypt=yes;"
    )


def test_connection_string_dsn_env_escape_hatch(monkeypatch):
    monkeypatch.setenv("MY_DSN", "Server=x;Database=y;")
    src = Source(name="warehouse", driver="mssql", dsn_env="MY_DSN")
    assert src.connection_string() == "Server=x;Database=y;"


def test_dsn_env_unset_raises(monkeypatch):
    monkeypatch.delenv("MY_DSN", raising=False)
    src = Source(name="warehouse", driver="mssql", dsn_env="MY_DSN")
    with pytest.raises(ConfigError, match="MY_DSN"):
        src.connection_string()


def test_password_env_unset_raises(monkeypatch):
    monkeypatch.delenv("WH_PWD", raising=False)
    src = make_source(auth=Auth(user="sa", password_env="WH_PWD"))
    with pytest.raises(ConfigError, match="WH_PWD"):
        src.connection_string()


def test_missing_auth_raises():
    src = make_source()  # no auth at all
    with pytest.raises(ConfigError, match="auth"):
        src.connection_string()


def test_missing_server_raises():
    src = Source(name="warehouse", driver="mssql", database="db",
                 auth=Auth(trusted=True))
    with pytest.raises(ConfigError, match="server"):
        src.connection_string()
```

**Step 2: Run** — `uv run pytest tests/test_config_sources.py -v` → FAIL.

**Step 3: Implement (start of `src/wh/config.py`)**

```python
"""wh.yaml loading, validation, and discovery."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .errors import ConfigError

DEFAULT_BATCH_SIZE = 100_000
VALID_MODES = {"native", "parquet"}
VALID_DRIVERS = {"mssql"}


@dataclass
class Auth:
    user: str | None = None
    password_env: str | None = None
    trusted: bool = False


@dataclass
class Source:
    name: str
    driver: str
    server: str | None = None
    database: str | None = None
    encrypt: bool = True
    trust_server_certificate: bool = False
    auth: Auth = field(default_factory=Auth)
    dsn_env: str | None = None

    def connection_string(self) -> str:
        """Assemble the driver connection string. Secrets come from env vars
        at call time, so a config can be parsed without them set."""
        if self.dsn_env:
            dsn = os.environ.get(self.dsn_env)
            if not dsn:
                raise ConfigError(
                    f"source '{self.name}': env var {self.dsn_env} is not set"
                )
            return dsn
        if not self.server or not self.database:
            raise ConfigError(
                f"source '{self.name}': needs server and database (or dsn_env)"
            )
        parts = [f"Server={self.server}", f"Database={self.database}"]
        if self.auth.trusted:
            parts.append("Trusted_Connection=yes")
        elif self.auth.user and self.auth.password_env:
            pwd = os.environ.get(self.auth.password_env)
            if not pwd:
                raise ConfigError(
                    f"source '{self.name}': env var {self.auth.password_env} is not set"
                )
            parts += [f"UID={self.auth.user}", f"PWD={pwd}"]
        else:
            raise ConfigError(
                f"source '{self.name}': auth needs 'trusted: true' or "
                f"'user' + 'password_env'"
            )
        parts.append(f"Encrypt={'yes' if self.encrypt else 'no'}")
        if self.trust_server_certificate:
            parts.append("TrustServerCertificate=yes")
        return ";".join(parts) + ";"
```

**Step 4: Run** → PASS.

**Step 5: Commit** — `git commit -am "feat: named sources with connection string assembly"`

---

## Task 4: Full config parsing

Port the POC's table/destination parsing into the new format.

**Files:**
- Modify: `src/wh/config.py`
- Test: `tests/test_config_load.py`

**Step 1: Write the failing tests**

```python
import pytest
import yaml

from wh.config import load_config
from wh.errors import ConfigError

VALID = {
    "sources": {
        "warehouse": {
            "driver": "mssql",
            "server": "localhost,1433",
            "database": "ExecReporting",
            "auth": {"user": "sa", "password_env": "WH_PWD"},
        },
        "other": {"driver": "mssql", "dsn_env": "OTHER_DSN"},
    },
    "destination": {"duckdb_path": "./metrics.duckdb"},
    "defaults": {"schema_in_duckdb": "core", "mode": "native"},
    "tables": [
        {
            "name": "waitlist",
            "description": "Current waitlist",
            "source": {"database": "ExecReporting", "schema": "dbo", "table": "Waits"},
        },
        {
            "name": "snapshot",
            "schema_in_duckdb": "main",
            "mode": "parquet",
            "source": {"query": "SELECT 1 AS x"},
        },
    ],
}


def write(tmp_path, cfg_dict):
    p = tmp_path / "wh.yaml"
    p.write_text(yaml.safe_dump(cfg_dict))
    return p


def test_valid_config(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert set(cfg.sources) == {"warehouse", "other"}
    assert cfg.default_source == "warehouse"          # first listed
    assert cfg.duckdb_path == (tmp_path / "metrics.duckdb").resolve()
    assert cfg.parquet_dir == (tmp_path / "parquet").resolve()  # default: sibling
    t0, t1 = cfg.tables
    assert (t0.name, t0.schema_in_duckdb, t0.mode) == ("waitlist", "core", "native")
    assert t0.source.sql() == "SELECT * FROM [ExecReporting].[dbo].[Waits]"
    assert (t1.name, t1.schema_in_duckdb, t1.mode) == ("snapshot", "main", "parquet")
    assert t1.source.sql() == "SELECT 1 AS x"


def test_relative_paths_resolve_against_config_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path.parent)  # cwd != config dir
    cfg = load_config(write(tmp_path, VALID))
    assert cfg.duckdb_path.parent == tmp_path


def test_no_sources_raises(tmp_path):
    bad = {**VALID, "sources": {}}
    with pytest.raises(ConfigError, match="source"):
        load_config(write(tmp_path, bad))


def test_unknown_driver_raises(tmp_path):
    bad = {**VALID, "sources": {"w": {"driver": "postgres", "dsn_env": "X"}}}
    with pytest.raises(ConfigError, match="driver"):
        load_config(write(tmp_path, bad))


def test_table_query_and_ref_mutually_exclusive(tmp_path):
    bad = dict(VALID)
    bad["tables"] = [{
        "name": "x",
        "source": {"query": "SELECT 1", "database": "a", "schema": "b", "table": "c"},
    }]
    with pytest.raises(ConfigError, match="either"):
        load_config(write(tmp_path, bad))


def test_duplicate_table_raises(tmp_path):
    bad = dict(VALID)
    bad["tables"] = [VALID["tables"][0], VALID["tables"][0]]
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(write(tmp_path, bad))


def test_no_tables_raises(tmp_path):
    bad = {**VALID, "tables": []}
    with pytest.raises(ConfigError, match="no tables"):
        load_config(write(tmp_path, bad))
```

**Step 2: Run** → FAIL (`load_config` doesn't exist).

**Step 3: Implement (append to `src/wh/config.py`)**

The `SourceRef`/`TableSpec` classes are ports of the POC (`mirror.py:44-82`); parsing is the POC loader (`mirror.py:92-168`) adapted to named sources and config-relative paths.

```python
@dataclass
class SourceRef:
    query: str | None = None
    database: str | None = None
    schema: str | None = None
    table: str | None = None

    def sql(self) -> str:
        if self.query:
            return self.query
        return f"SELECT * FROM [{self.database}].[{self.schema}].[{self.table}]"

    def validate(self, name: str) -> None:
        if self.query:
            if any([self.database, self.schema, self.table]):
                raise ConfigError(
                    f"table '{name}': give either 'query' OR database/schema/table, not both"
                )
        elif not all([self.database, self.schema, self.table]):
            raise ConfigError(
                f"table '{name}': needs 'query' or all of database/schema/table"
            )


@dataclass
class TableSpec:
    name: str
    source: SourceRef
    schema_in_duckdb: str = "main"
    description: str | None = None
    mode: str = "native"
    compression: str = "zstd"
    compression_level: int = 9
    batch_size: int = DEFAULT_BATCH_SIZE

    @property
    def qualified(self) -> str:
        return f'"{self.schema_in_duckdb}"."{self.name}"'


@dataclass
class Config:
    path: Path                    # the wh.yaml this was loaded from
    sources: dict[str, Source]
    default_source: str           # first source listed
    duckdb_path: Path
    parquet_dir: Path
    tables: list[TableSpec] = field(default_factory=list)


def _parse_source(name: str, raw: dict) -> Source:
    driver = raw.get("driver", "mssql")
    if driver not in VALID_DRIVERS:
        raise ConfigError(
            f"source '{name}': driver must be one of {sorted(VALID_DRIVERS)}"
        )
    a = raw.get("auth") or {}
    return Source(
        name=name,
        driver=driver,
        server=raw.get("server"),
        database=raw.get("database"),
        encrypt=bool(raw.get("encrypt", True)),
        trust_server_certificate=bool(raw.get("trust_server_certificate", False)),
        auth=Auth(
            user=a.get("user"),
            password_env=a.get("password_env"),
            trusted=bool(a.get("trusted", False)),
        ),
        dsn_env=raw.get("dsn_env"),
    )


def load_config(path: Path | str) -> Config:
    path = Path(path).resolve()
    with open(path) as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    base = path.parent

    raw_sources = raw.get("sources") or {}
    if not raw_sources:
        raise ConfigError("at least one entry under 'sources:' is required")
    sources = {n: _parse_source(n, s or {}) for n, s in raw_sources.items()}
    default_source = next(iter(sources))

    dst = raw.get("destination") or {}
    dfl = raw.get("defaults") or {}

    duckdb_path = dst.get("duckdb_path")
    if not duckdb_path:
        raise ConfigError("destination.duckdb_path is required")
    duckdb_path = (base / duckdb_path).resolve()
    parquet_dir = (base / dst.get("parquet_dir", duckdb_path.parent / "parquet")).resolve()

    default_mode = dfl.get("mode", "native")
    if default_mode not in VALID_MODES:
        raise ConfigError(f"defaults.mode must be one of {sorted(VALID_MODES)}")

    tables: list[TableSpec] = []
    seen: set[tuple[str, str]] = set()
    for i, t in enumerate(raw.get("tables") or []):
        name = t.get("name")
        if not name:
            raise ConfigError(f"tables[{i}]: 'name' is required")
        s = t.get("source") or {}
        source = SourceRef(
            query=s.get("query"),
            database=s.get("database"),
            schema=s.get("schema"),
            table=s.get("table"),
        )
        source.validate(name)
        mode = t.get("mode", default_mode)
        if mode not in VALID_MODES:
            raise ConfigError(f"table '{name}': mode must be one of {sorted(VALID_MODES)}")
        extract = t.get("extract") or {}
        spec = TableSpec(
            name=name,
            source=source,
            schema_in_duckdb=t.get("schema_in_duckdb", dfl.get("schema_in_duckdb", "main")),
            description=t.get("description"),
            mode=mode,
            compression=t.get("compression", dfl.get("compression", "zstd")),
            compression_level=int(t.get("compression_level", dfl.get("compression_level", 9))),
            batch_size=int(extract.get("batch_size", dfl.get("batch_size", DEFAULT_BATCH_SIZE))),
        )
        key = (spec.schema_in_duckdb, spec.name)
        if key in seen:
            raise ConfigError(f"duplicate table {spec.schema_in_duckdb}.{spec.name}")
        seen.add(key)
        tables.append(spec)
    if not tables:
        raise ConfigError("no tables defined")

    return Config(
        path=path,
        sources=sources,
        default_source=default_source,
        duckdb_path=duckdb_path,
        parquet_dir=parquet_dir,
        tables=tables,
    )
```

**Step 4: Run** — `uv run pytest tests/test_config_load.py -v` → PASS. Also run the full suite: `uv run pytest` → all PASS.

**Step 5: Commit** — `git commit -am "feat: wh.yaml parsing with named sources and config-relative paths"`

---

## Task 5: Config discovery

**Files:**
- Modify: `src/wh/config.py`
- Test: `tests/test_config_find.py`

**Step 1: Write the failing tests**

```python
import pytest

from wh.config import find_config
from wh.errors import ConfigError


def test_finds_in_cwd(tmp_path, monkeypatch):
    (tmp_path / "wh.yaml").write_text("x: 1")
    monkeypatch.chdir(tmp_path)
    assert find_config() == tmp_path / "wh.yaml"


def test_walks_up(tmp_path, monkeypatch):
    (tmp_path / "wh.yaml").write_text("x: 1")
    deep = tmp_path / "notebooks" / "july"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert find_config() == tmp_path / "wh.yaml"


def test_not_found_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="wh.yaml"):
        find_config()
```

**Step 2: Run** → FAIL.

**Step 3: Implement (append to `src/wh/config.py`)**

```python
def find_config(start: Path | None = None) -> Path:
    """Walk up from `start` (default cwd) looking for wh.yaml."""
    cur = (start or Path.cwd()).resolve()
    for p in (cur, *cur.parents):
        candidate = p / "wh.yaml"
        if candidate.is_file():
            return candidate
    raise ConfigError(
        f"no wh.yaml found in {cur} or any parent directory "
        f"(create one, or pass an explicit path)"
    )
```

**Step 4: Run** → PASS.

**Step 5: Commit** — `git commit -am "feat: wh.yaml discovery by walking up from cwd"`

---

## Task 6: Workspace + module-level API

**Files:**
- Create: `src/wh/workspace.py`
- Modify: `src/wh/__init__.py`
- Test: `tests/test_workspace.py`
- Test fixture: `tests/conftest.py`

**Step 1: Create `tests/conftest.py`** (shared by later tasks too)

```python
import pytest
import yaml


@pytest.fixture
def project(tmp_path):
    """A minimal project dir with a valid wh.yaml. Returns its path."""
    cfg = {
        "sources": {
            "warehouse": {
                "driver": "mssql",
                "server": "localhost,1433",
                "database": "Db",
                "auth": {"user": "sa", "password_env": "WH_PWD"},
            }
        },
        "destination": {"duckdb_path": "./metrics.duckdb"},
        "tables": [
            {"name": "t1", "source": {"query": "SELECT 1 AS a"}},
        ],
    }
    (tmp_path / "wh.yaml").write_text(yaml.safe_dump(cfg))
    return tmp_path
```

**Step 2: Write the failing tests**

```python
import duckdb

import wh
from wh.workspace import Workspace


def test_load_explicit_path(project):
    ws = Workspace.load(project / "wh.yaml")
    assert ws.config.duckdb_path == project / "metrics.duckdb"


def test_connect_creates_and_queries(project):
    ws = Workspace.load(project / "wh.yaml")
    con = ws.connect()
    assert con.execute("SELECT 42").fetchone() == (42,)
    con.close()
    assert (project / "metrics.duckdb").exists()


def test_module_level_uses_discovery(project, monkeypatch):
    monkeypatch.chdir(project / "sub" if (project / "sub").mkdir() else project / "sub")
    monkeypatch.setattr(wh, "_default", None)  # reset the lazy singleton
    con = wh.connect()
    assert con.execute("SELECT 1").fetchone() == (1,)
    con.close()


def test_module_workspace_explicit_path_bypasses_singleton(project):
    ws = wh.workspace(project / "wh.yaml")
    assert isinstance(ws, Workspace)
```

**Step 3: Run** — `uv run pytest tests/test_workspace.py -v` → FAIL.

**Step 4: Implement `src/wh/workspace.py`**

```python
"""Workspace — the hub everything hangs off."""

from __future__ import annotations

from pathlib import Path

import duckdb

from .config import Config, find_config, load_config


class Workspace:
    def __init__(self, config: Config):
        self.config = config

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Workspace":
        cfg_path = Path(path) if path is not None else find_config()
        return cls(load_config(cfg_path))

    def connect(self, read_only: bool = False) -> duckdb.DuckDBPyConnection:
        """Connection to the local mirror. Pass read_only=True when several
        notebooks share the file."""
        return duckdb.connect(str(self.config.duckdb_path), read_only=read_only)
```

**Step 5: Implement `src/wh/__init__.py`**

```python
"""wh — helpers for mirroring warehouse data into DuckDB and back.

Simple by default:

    import wh
    con = wh.connect()      # duckdb connection to the local mirror
    wh.mirror()             # full refresh
    wh.freshness()          # how stale is each table?

`wh.workspace()` returns the underlying Workspace for anything fancier.
"""

from __future__ import annotations

from pathlib import Path

from .errors import ConfigError, PushRefused, SchemaMismatch, SourceError, WhError
from .workspace import Workspace

__all__ = [
    "Workspace", "workspace", "connect", "mirror", "freshness",
    "WhError", "ConfigError", "SourceError", "PushRefused", "SchemaMismatch",
]

_default: Workspace | None = None


def workspace(path: str | Path | None = None) -> Workspace:
    """The default workspace (discovered wh.yaml), or an explicit one."""
    global _default
    if path is not None:
        return Workspace.load(path)
    if _default is None:
        _default = Workspace.load()
    return _default


def connect(**kwargs):
    return workspace().connect(**kwargs)


def mirror(**kwargs):
    return workspace().mirror(**kwargs)


def freshness():
    return workspace().freshness()
```

(`mirror`/`freshness` on Workspace arrive in Tasks 7–11; the module-level wrappers are written once here.)

**Step 6: Run** — `uv run pytest tests/test_workspace.py -v` → PASS. Full suite → PASS.

**Step 7: Commit** — `git commit -am "feat: Workspace and module-level connect/workspace API"`

---

## Task 7: Mirror build — native mode + meta table

The extraction seam: `build(cfg, extract, ...)` where `extract(spec)` returns an Arrow table/reader. Tests never need SQL Server.

**Files:**
- Create: `src/wh/mirror.py`
- Test: `tests/test_mirror.py`
- Modify: `tests/conftest.py` (add helpers)

**Step 1: Add helpers to `tests/conftest.py`**

```python
from pathlib import Path

import pyarrow as pa

from wh.config import Config, Source, Auth, SourceRef, TableSpec


def make_spec(name, schema="main", mode="native", **kw):
    return TableSpec(
        name=name, source=SourceRef(query=f"SELECT * FROM {name}"),
        schema_in_duckdb=schema, mode=mode, **kw,
    )


def make_config(tmp_path, specs):
    return Config(
        path=tmp_path / "wh.yaml",
        sources={"warehouse": Source(name="warehouse", driver="mssql", dsn_env="X")},
        default_source="warehouse",
        duckdb_path=tmp_path / "metrics.duckdb",
        parquet_dir=tmp_path / "parquet",
        tables=specs,
    )


def fake_extract(data: dict):
    """extract seam returning canned pyarrow tables by spec name."""
    def _extract(spec):
        return pa.table(data[spec.name])
    return _extract
```

**Step 2: Write the failing tests (`tests/test_mirror.py`)**

```python
import duckdb
import pytest

from wh.mirror import build
from tests.conftest import make_config, make_spec, fake_extract


def q(cfg, sql):
    con = duckdb.connect(str(cfg.duckdb_path), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_native_table_lands_with_meta(tmp_path):
    cfg = make_config(tmp_path, [make_spec("t1")])
    build(cfg, fake_extract({"t1": {"a": [1, 2, 3]}}))

    assert q(cfg, 'SELECT a FROM "main"."t1" ORDER BY a') == [(1,), (2,), (3,)]
    meta = q(cfg, "SELECT schema_name, table_name, mode, row_count FROM _mirror.meta")
    assert meta == [("main", "t1", "native", 3)]
    ts = q(cfg, "SELECT extracted_at FROM _mirror.meta")[0][0]
    assert ts is not None


def test_description_becomes_comment(tmp_path):
    cfg = make_config(tmp_path, [make_spec("t1", description="my table")])
    build(cfg, fake_extract({"t1": {"a": [1]}}))
    rows = q(cfg, "SELECT comment FROM duckdb_tables() WHERE table_name = 't1'")
    assert rows == [("my table",)]


def test_staging_cleaned_up(tmp_path):
    cfg = make_config(tmp_path, [make_spec("t1")])
    build(cfg, fake_extract({"t1": {"a": [1]}}))
    assert not (tmp_path / ".mirror_staging").exists()
```

**Step 3: Run** → FAIL (`wh.mirror` doesn't exist).

**Step 4: Implement `src/wh/mirror.py`**

This is the POC's `run()`/`load_table()` (old `mirror.py:207-343`) restructured around the seam, plus the meta table. Written here in full — later tasks extend it.

```python
"""Mirror build: extract via a seam into a staging .duckdb, atomically swap.

The extract seam — `extract(spec) -> Arrow table/reader` — keeps all of
this testable without a warehouse. The live .duckdb and parquet dir are
never touched until the whole build has succeeded.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import duckdb

from .config import Config, TableSpec
from .errors import ConfigError

Extract = Callable[[TableSpec], Any]

META_DDL = """
CREATE SCHEMA IF NOT EXISTS _mirror;
CREATE TABLE IF NOT EXISTS _mirror.meta (
    schema_name  VARCHAR,
    table_name   VARCHAR,
    source_sql   VARCHAR,
    mode         VARCHAR,
    row_count    BIGINT,
    extracted_at TIMESTAMPTZ,
    duration_s   DOUBLE,
    spec_hash    VARCHAR
);
"""


def sql_str(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def spec_hash(spec: TableSpec) -> str:
    payload = json.dumps(asdict(spec), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _load_table(
    con: duckdb.DuckDBPyConnection,
    extract: Extract,
    spec: TableSpec,
    staging_parquet_dir: Path,
    final_parquet_dir: Path,
) -> tuple[int, Path | None]:
    """Extract one table into staging. Returns (row_count, final_parquet_path_or_None).

    Parquet views are NOT created here: DuckDB binds read_parquet() at
    CREATE VIEW time, so the file must already exist at its final path.
    """
    con.execute(f'CREATE SCHEMA IF NOT EXISTS "{spec.schema_in_duckdb}"')
    src = extract(spec)
    con.register("_mirror_src", src)
    try:
        if spec.mode == "native":
            con.execute(f"CREATE TABLE {spec.qualified} AS SELECT * FROM _mirror_src")
            if spec.description:
                con.execute(
                    f"COMMENT ON TABLE {spec.qualified} IS {sql_str(spec.description)}"
                )
            (count,) = con.execute(f"SELECT count(*) FROM {spec.qualified}").fetchone()
            return count, None

        rel_path = Path(spec.schema_in_duckdb) / f"{spec.name}.parquet"
        staging_file = staging_parquet_dir / rel_path
        staging_file.parent.mkdir(parents=True, exist_ok=True)
        con.execute(
            f"COPY (SELECT * FROM _mirror_src) TO {sql_str(str(staging_file))} "
            f"(FORMAT PARQUET, COMPRESSION {spec.compression}, "
            f"COMPRESSION_LEVEL {spec.compression_level})"
        )
    finally:
        con.unregister("_mirror_src")

    (count,) = con.execute(
        f"SELECT count(*) FROM read_parquet({sql_str(str(staging_file))})"
    ).fetchone()
    return count, final_parquet_dir / rel_path


def _write_meta(con, spec: TableSpec, rows: int, duration: float) -> None:
    con.execute(
        "INSERT INTO _mirror.meta VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            spec.schema_in_duckdb, spec.name, spec.source.sql(), spec.mode,
            rows, datetime.now(timezone.utc), round(duration, 3), spec_hash(spec),
        ],
    )


def _create_view(con, spec: TableSpec, final_file: Path) -> None:
    con.execute(
        f"CREATE VIEW {spec.qualified} AS "
        f"SELECT * FROM read_parquet({sql_str(str(final_file))})"
    )
    if spec.description:
        con.execute(f"COMMENT ON VIEW {spec.qualified} IS {sql_str(spec.description)}")


def build(
    cfg: Config,
    extract: Extract,
    *,
    only: list[str] | None = None,
    keep_staging: bool = False,
    log: Callable[[str], None] = print,
) -> None:
    tables = cfg.tables
    if only:
        wanted = set(only)
        missing = wanted - {t.name for t in cfg.tables}
        if missing:
            raise ConfigError(f"--only names not in config: {sorted(missing)}")
        tables = [t for t in cfg.tables if t.name in wanted]

    staging_root = cfg.duckdb_path.parent / ".mirror_staging"
    if staging_root.exists():
        shutil.rmtree(staging_root)
    staging_root.mkdir(parents=True)
    staging_db = staging_root / cfg.duckdb_path.name
    staging_parquet = staging_root / "parquet"

    con = duckdb.connect(str(staging_db))
    t0 = time.time()
    try:
        con.execute(META_DDL)

        # ---- phase 1: extract into staging ----
        pending_views: list[tuple[TableSpec, Path]] = []
        for spec in tables:
            start = time.time()
            rows, final_file = _load_table(
                con, extract, spec, staging_parquet, cfg.parquet_dir
            )
            _write_meta(con, spec, rows, time.time() - start)
            if final_file is not None:
                pending_views.append((spec, final_file))
            log(
                f"  {spec.schema_in_duckdb}.{spec.name:<30} "
                f"{spec.mode:<8} {rows:>12,} rows  {time.time() - start:6.1f}s"
            )

        # ---- phase 2: swap parquet dir (before views: read_parquet binds
        # at CREATE VIEW time, files must exist at final paths) ----
        if pending_views:
            old = cfg.parquet_dir.with_name(cfg.parquet_dir.name + ".old")
            if old.exists():
                shutil.rmtree(old)
            if cfg.parquet_dir.exists():
                cfg.parquet_dir.rename(old)
            staging_parquet.rename(cfg.parquet_dir)
            if old.exists():
                shutil.rmtree(old)

        # ---- phase 3: parquet-backed views ----
        for spec, final_file in pending_views:
            _create_view(con, spec, final_file)
        con.close()
        con = None

        # ---- phase 4: swap the db file ----
        os.replace(staging_db, cfg.duckdb_path)
        log(f"Mirror -> {cfg.duckdb_path}  (total {time.time() - t0:.1f}s)")
    finally:
        if con is not None:
            con.close()
        if staging_root.exists() and not keep_staging:
            shutil.rmtree(staging_root, ignore_errors=True)
```

**Step 5: Run** — `uv run pytest tests/test_mirror.py -v` → PASS.

**Step 6: Commit** — `git commit -am "feat: mirror build with extract seam and _mirror.meta"`

---

## Task 8: Mirror build — parquet mode

**Files:**
- Test: `tests/test_mirror.py` (add)

**Step 1: Write the failing test**

```python
def test_parquet_mode_creates_file_and_view(tmp_path):
    cfg = make_config(tmp_path, [make_spec("t2", mode="parquet")])
    build(cfg, fake_extract({"t2": {"b": ["x", "y"]}}))

    pq = cfg.parquet_dir / "main" / "t2.parquet"
    assert pq.exists()
    assert q(cfg, 'SELECT b FROM "main"."t2" ORDER BY b') == [("x",), ("y",)]
    assert q(cfg, "SELECT mode, row_count FROM _mirror.meta") == [("parquet", 2)]
```

**Step 2: Run** — should PASS already (Task 7 implemented parquet mode). If it fails, fix before moving on. This test exists to lock the behaviour.

**Step 3: Commit** — `git commit -am "test: parquet-mode mirror coverage"`

---

## Task 9: Mirror build — failure atomicity

**Files:**
- Test: `tests/test_mirror.py` (add)

**Step 1: Write the failing test**

```python
def test_failed_build_leaves_live_mirror_untouched(tmp_path):
    cfg = make_config(tmp_path, [make_spec("t1"), make_spec("t2")])
    build(cfg, fake_extract({"t1": {"a": [1]}, "t2": {"a": [2]}}))

    def exploding(spec):
        if spec.name == "t2":
            raise RuntimeError("warehouse hiccup")
        import pyarrow as pa
        return pa.table({"a": [99]})

    with pytest.raises(RuntimeError):
        build(cfg, exploding)

    # old data still intact, staging cleaned up
    assert q(cfg, 'SELECT a FROM "main"."t1"') == [(1,)]
    assert q(cfg, 'SELECT a FROM "main"."t2"') == [(2,)]
    assert not (tmp_path / ".mirror_staging").exists()
```

**Step 2: Run** — should PASS (the POC design already guarantees this). Locks the invariant.

**Step 3: Commit** — `git commit -am "test: failed builds never touch the live mirror"`

---

## Task 10: Carry-over `--only`

`only=` must always produce a **complete** mirror: refresh the selected tables, carry everything else over from the existing mirror (native: copy data; parquet: copy file). Tables missing from the previous mirror are extracted fresh. Meta rows for carried tables are preserved.

**Files:**
- Modify: `src/wh/mirror.py`
- Test: `tests/test_mirror_only.py`

**Step 1: Write the failing tests**

```python
import duckdb
import pytest

from wh.mirror import build
from tests.conftest import make_config, make_spec, fake_extract
from tests.test_mirror import q


def test_only_refreshes_selected_and_carries_rest(tmp_path):
    cfg = make_config(tmp_path, [make_spec("t1"), make_spec("t2")])
    build(cfg, fake_extract({"t1": {"a": [1]}, "t2": {"a": [10]}}))
    first_meta = dict(q(cfg, "SELECT table_name, extracted_at FROM _mirror.meta"))

    build(cfg, fake_extract({"t1": {"a": [2]}}), only=["t1"])

    assert q(cfg, 'SELECT a FROM "main"."t1"') == [(2,)]      # refreshed
    assert q(cfg, 'SELECT a FROM "main"."t2"') == [(10,)]     # carried over
    second_meta = dict(q(cfg, "SELECT table_name, extracted_at FROM _mirror.meta"))
    assert second_meta["t2"] == first_meta["t2"]              # meta preserved
    assert second_meta["t1"] > first_meta["t1"]               # meta refreshed


def test_only_carries_parquet_files(tmp_path):
    cfg = make_config(
        tmp_path, [make_spec("t1"), make_spec("t2", mode="parquet")]
    )
    build(cfg, fake_extract({"t1": {"a": [1]}, "t2": {"b": ["x"]}}))

    build(cfg, fake_extract({"t1": {"a": [2]}}), only=["t1"])

    assert (cfg.parquet_dir / "main" / "t2.parquet").exists()
    assert q(cfg, 'SELECT b FROM "main"."t2"') == [("x",)]


def test_only_extracts_table_missing_from_previous(tmp_path):
    cfg1 = make_config(tmp_path, [make_spec("t1")])
    build(cfg1, fake_extract({"t1": {"a": [1]}}))

    # config grows a new table; --only t1 must still produce a complete mirror
    cfg2 = make_config(tmp_path, [make_spec("t1"), make_spec("t_new")])
    build(cfg2, fake_extract({"t1": {"a": [2]}, "t_new": {"a": [7]}}), only=["t1"])

    assert q(cfg2, 'SELECT a FROM "main"."t_new"') == [(7,)]


def test_only_without_existing_mirror_is_full_build(tmp_path):
    cfg = make_config(tmp_path, [make_spec("t1"), make_spec("t2")])
    build(cfg, fake_extract({"t1": {"a": [1]}, "t2": {"a": [2]}}), only=["t1"])
    assert q(cfg, 'SELECT a FROM "main"."t2"') == [(2,)]


def test_only_unknown_name_raises(tmp_path):
    from wh.errors import ConfigError
    cfg = make_config(tmp_path, [make_spec("t1")])
    with pytest.raises(ConfigError, match="nope"):
        build(cfg, fake_extract({"t1": {"a": [1]}}), only=["nope"])
```

**Step 2: Run** → FAIL (carry-over not implemented; e.g. t2 missing after `--only t1`).

**Step 3: Implement in `src/wh/mirror.py`**

Add a helper and rework the `only` branch of `build()`:

```python
def _prev_has(con, catalog_schema: str, name: str, schema: str) -> bool:
    (n,) = con.execute(
        "SELECT count(*) FROM prev.information_schema.tables "
        "WHERE table_schema = ? AND table_name = ?",
        [schema, name],
    ).fetchone()
    return n > 0
```

In `build()`, replace the `if only:` block and phase 1 with:

```python
    tables = cfg.tables
    carried: list[TableSpec] = []
    if only:
        wanted = set(only)
        missing = wanted - {t.name for t in cfg.tables}
        if missing:
            raise ConfigError(f"--only names not in config: {sorted(missing)}")
        tables = [t for t in cfg.tables if t.name in wanted]
        if cfg.duckdb_path.exists():
            carried = [t for t in cfg.tables if t.name not in wanted]
        # no previous mirror: fall through to a full build of `tables`
        else:
            tables = cfg.tables
```

and inside the `try:` after `con.execute(META_DDL)`:

```python
        # ---- phase 0: carry over unselected tables from the live mirror ----
        pending_views: list[tuple[TableSpec, Path]] = []
        if carried:
            con.execute(
                f"ATTACH {sql_str(str(cfg.duckdb_path))} AS prev (READ_ONLY)"
            )
            has_meta = con.execute(
                "SELECT count(*) FROM prev.information_schema.tables "
                "WHERE table_schema = '_mirror' AND table_name = 'meta'"
            ).fetchone()[0] > 0
            for spec in carried[:]:
                if spec.mode == "native":
                    if not _prev_has(con, "prev", spec.name, spec.schema_in_duckdb):
                        carried.remove(spec)
                        tables.append(spec)      # extract fresh instead
                        continue
                    con.execute(
                        f'CREATE SCHEMA IF NOT EXISTS "{spec.schema_in_duckdb}"'
                    )
                    con.execute(
                        f"CREATE TABLE {spec.qualified} "
                        f"AS SELECT * FROM prev.{spec.qualified}"
                    )
                    if spec.description:
                        con.execute(
                            f"COMMENT ON TABLE {spec.qualified} IS "
                            f"{sql_str(spec.description)}"
                        )
                else:  # parquet: copy the live file into staging
                    rel = Path(spec.schema_in_duckdb) / f"{spec.name}.parquet"
                    live_file = cfg.parquet_dir / rel
                    if not live_file.exists():
                        carried.remove(spec)
                        tables.append(spec)
                        continue
                    dst = staging_parquet / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(live_file, dst)
                    pending_views.append((spec, cfg.parquet_dir / rel))
                if has_meta:
                    con.execute(
                        "INSERT INTO _mirror.meta SELECT * FROM prev._mirror.meta "
                        "WHERE schema_name = ? AND table_name = ?",
                        [spec.schema_in_duckdb, spec.name],
                    )
                log(f"  {spec.schema_in_duckdb}.{spec.name:<30} carried over")
            con.execute("DETACH prev")
```

(Phase 1 then appends to the same `pending_views` list — remove its local re-declaration. Note `carried[:]` iteration since we mutate `carried`. The `pending_views` truthiness in phase 2 also needs to consider that carried parquet files must survive the swap — they're already in `staging_parquet`, so the existing swap logic is correct, but the swap must now trigger when `pending_views` is non-empty OR the staging parquet dir exists; simplest: `if staging_parquet.exists():`.)

**Step 4: Run** — `uv run pytest tests/test_mirror_only.py -v` → PASS. Full suite → PASS.

**Step 5: Commit** — `git commit -am "feat: --only carries over unrefreshed tables (always a complete mirror)"`

---

## Task 11: `Workspace.mirror()` and `freshness()`

**Files:**
- Modify: `src/wh/workspace.py`
- Test: `tests/test_workspace.py` (add)

**Step 1: Write the failing tests**

```python
def test_workspace_mirror_with_injected_extract(project):
    import pyarrow as pa
    ws = Workspace.load(project / "wh.yaml")
    ws.mirror(extract=lambda spec: pa.table({"a": [1]}), log=lambda s: None)
    con = ws.connect(read_only=True)
    assert con.execute('SELECT a FROM "main"."t1"').fetchall() == [(1,)]
    con.close()


def test_freshness(project):
    import pyarrow as pa
    ws = Workspace.load(project / "wh.yaml")
    ws.mirror(extract=lambda spec: pa.table({"a": [1]}), log=lambda s: None)
    fresh = ws.freshness()
    assert fresh.num_rows == 1
    assert "extracted_at" in fresh.column_names
```

**Step 2: Run** → FAIL (no `mirror`/`freshness` methods).

**Step 3: Implement (add to `Workspace`)**

```python
    def mirror(
        self,
        only: list[str] | None = None,
        *,
        extract=None,
        keep_staging: bool = False,
        log=print,
    ) -> None:
        """Refresh the local mirror. `extract` is injectable for tests;
        default is the mssql extractor for the config's default source."""
        from . import mirror as mirror_mod

        if extract is None:
            from .sources.mssql import MssqlExtractor

            source = self.config.sources[self.config.default_source]
            extractor = MssqlExtractor(source)
            try:
                mirror_mod.build(
                    self.config, extractor, only=only,
                    keep_staging=keep_staging, log=log,
                )
            finally:
                extractor.close()
        else:
            mirror_mod.build(
                self.config, extract, only=only,
                keep_staging=keep_staging, log=log,
            )

    def freshness(self):
        """One row per mirrored table: mode, row_count, extracted_at, duration_s."""
        con = self.connect(read_only=True)
        try:
            return con.execute(
                "SELECT schema_name, table_name, mode, row_count, "
                "extracted_at, duration_s "
                "FROM _mirror.meta ORDER BY schema_name, table_name"
            ).arrow()
        finally:
            con.close()
```

**Step 4: Run** — `uv run pytest tests/test_workspace.py -v` → PASS (mssql import path untested here — Task 12).

**Step 5: Commit** — `git commit -am "feat: Workspace.mirror() and freshness()"`

---

## Task 12: MssqlExtractor + CLI

**Files:**
- Create: `src/wh/sources/__init__.py` (empty), `src/wh/sources/mssql.py`, `src/wh/cli.py`
- Test: `tests/test_mssql_integration.py`, `tests/test_cli.py`

**Step 1: Write the failing tests**

`tests/test_cli.py`:

```python
import pytest

from wh.cli import main


def test_validate_ok(project, capsys):
    assert main(["validate", "--config", str(project / "wh.yaml")]) == 0
    assert "OK: 1 tables" in capsys.readouterr().out


def test_validate_bad_config(tmp_path, capsys):
    (tmp_path / "wh.yaml").write_text("sources: {}")
    assert main(["validate", "--config", str(tmp_path / "wh.yaml")]) == 2
    assert "error:" in capsys.readouterr().err


def test_no_config_found(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["validate"]) == 2
```

`tests/test_mssql_integration.py` (opt-in — skipped without a server):

```python
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("WH_TEST_DSN"),
    reason="set WH_TEST_DSN to run SQL Server integration tests",
)


def test_extract_roundtrip(tmp_path):
    from wh.config import Source, SourceRef, TableSpec
    from wh.sources.mssql import MssqlExtractor
    import pyarrow as pa

    src = Source(name="test", driver="mssql", dsn_env="WH_TEST_DSN")
    ex = MssqlExtractor(src)
    try:
        spec = TableSpec(name="probe", source=SourceRef(query="SELECT 1 AS n"))
        result = ex(spec)
        table = pa.table(result) if not isinstance(result, pa.Table) else result
        assert table.column("n").to_pylist() == [1]
    finally:
        ex.close()
```

**Step 2: Run** — cli tests FAIL, integration test SKIPS.

**Step 3: Implement `src/wh/sources/mssql.py`**

```python
"""SQL Server extraction via mssql-python's Arrow API."""

from __future__ import annotations

from ..config import Source, TableSpec
from ..errors import SourceError


class MssqlExtractor:
    """Implements the mirror extract seam: extractor(spec) -> Arrow.

    Keeps one connection; the cursor for a streaming arrow_reader must stay
    open while DuckDB consumes it, so it's closed on the next call / close().
    """

    def __init__(self, source: Source):
        try:
            import mssql_python
        except ImportError as e:
            raise SourceError(
                "mssql-python is not installed (uv add mssql-python)"
            ) from e
        try:
            self._conn = mssql_python.connect(source.connection_string())
        except Exception as e:
            raise SourceError(
                f"could not connect to source '{source.name}': {e}"
            ) from e
        self._cursor = None

    def __call__(self, spec: TableSpec):
        if self._cursor is not None:
            self._cursor.close()
            self._cursor = None
        cursor = self._conn.cursor()
        try:
            cursor.execute(spec.source.sql())
        except Exception as e:
            cursor.close()
            raise SourceError(f"extract failed for table '{spec.name}': {e}") from e
        self._cursor = cursor
        if hasattr(cursor, "arrow_reader"):
            return cursor.arrow_reader(batch_size=spec.batch_size)
        return cursor.arrow()

    def close(self) -> None:
        if self._cursor is not None:
            self._cursor.close()
        self._conn.close()
```

**Step 4: Implement `src/wh/cli.py`**

```python
"""wh CLI: `wh validate`, `wh mirror`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import find_config, load_config
from .errors import WhError
from .workspace import Workspace


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wh", description="DuckDB warehouse tools")
    sub = p.add_subparsers(dest="command", required=True)

    pv = sub.add_parser("validate", help="parse the config and exit")
    pv.add_argument("--config", type=Path, help="path to wh.yaml (default: discover)")

    pm = sub.add_parser("mirror", help="refresh the local mirror")
    pm.add_argument("--config", type=Path, help="path to wh.yaml (default: discover)")
    pm.add_argument("--only", action="append", metavar="TABLE",
                    help="refresh only these tables (others carried over)")
    pm.add_argument("--keep-staging", action="store_true")

    args = p.parse_args(argv)
    try:
        cfg = load_config(args.config or find_config())
        if args.command == "validate":
            print(f"OK: {len(cfg.tables)} tables -> {cfg.duckdb_path}")
            return 0
        Workspace(cfg).mirror(only=args.only, keep_staging=args.keep_staging)
        return 0
    except WhError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
```

**Step 5: Run** — `uv run pytest -v` → all PASS (integration skipped). Then a smoke check: `uv run wh --help` and, with your local server up and `export WH_TEST_DSN="Server=localhost,1433;Database=ExecReporting;UID=sa;PWD=...;Encrypt=no;TrustServerCertificate=yes;"`, run `uv run pytest tests/test_mssql_integration.py -v` → PASS.

**Step 6: Commit** — `git commit -am "feat: mssql extractor and wh CLI (validate, mirror)"`

---## Task 13: Repo migration + docs

**Files:**
- Create: `wh.yaml` (replaces `config.yaml`), `CLAUDE.md`
- Modify: `README.md`
- Delete: `mirror.py`, `config.yaml`

**Step 1: Write `wh.yaml`** (port of config.yaml to the new format; password env var, not the DSN)

```yaml
sources:
  warehouse:
    driver: mssql
    server: localhost,1433
    database: ExecReporting
    encrypt: false
    trust_server_certificate: true
    auth:
      user: sa
      password_env: WH_WAREHOUSE_PWD

destination:
  duckdb_path: ./metrics.duckdb
  parquet_dir: ./parquet

defaults:
  mode: native
  schema_in_duckdb: core
  compression: zstd
  compression_level: 9
  batch_size: 100000

tables:
  - name: outpatient_waitlist_current
    description: "Current Outpatient Waitlist"
    schema_in_duckdb: main
    source:
      database: ExecReporting
      schema: dbo
      table: OutpatientWaitsCurrent

  - name: outpatient_waitlist_snapshot
    description: "Daily waitlist snapshot"
    schema_in_duckdb: main
    source:
      query: >
        SELECT * FROM [ExecReporting].[dbo].[OutpatientWaitsPatientLevel]
```

**Step 2: Delete the POC** — `git rm mirror.py config.yaml`

**Step 3: Verify against the real server** (this is the phase-1 acceptance test)

```bash
export WH_WAREHOUSE_PWD='<the sa password>'
uv run wh validate
uv run wh mirror
uv run wh mirror --only outpatient_waitlist_current   # carry-over check
uv run python -c "import wh; print(wh.freshness())"
```

Expected: validate OK, both tables mirrored with row counts, `--only` output shows one "carried over" line, freshness prints two meta rows.

**Step 4: Update `README.md`** — short quickstart: install (`uv add git+<repo-url>` or path), `wh.yaml` example (copy the one above), the five-verb API sketch, CLI usage, pointer to `docs/plans/` for design.

**Step 5: Write `CLAUDE.md`** (working notes for future sessions)

```markdown
# warehouse-tools (wh)

Helper library: warehouse data -> local DuckDB mirror -> analysis in
marimo/jupyter -> results pushed back to SQL Server.

## Commands
- `uv run pytest` — full suite (SQL Server tests auto-skip)
- `WH_TEST_DSN='...' uv run pytest tests/test_mssql_integration.py` — integration
- `uv run wh validate` / `uv run wh mirror [--only t]` — CLI
- Local dev SQL Server on localhost,1433 (see wh.yaml; password env WH_WAREHOUSE_PWD)

## Architecture
- Design: docs/plans/2026-07-19-warehouse-tools-design.md (read first)
- Phase 1 plan: docs/plans/2026-07-19-warehouse-tools-phase1.md
- Key seam: `mirror.build(cfg, extract)` — extract(spec) -> Arrow. All mirror
  tests inject fakes; only MssqlExtractor touches a real server.
- Config: wh.yaml discovered by walking up from cwd; relative paths resolve
  against the config file's dir, NOT cwd.
- `_mirror.meta` table in the .duckdb records freshness per table.

## Conventions
- TDD: test first, watch it fail, implement, commit per task.
- Simple-by-default API: five verbs (connect/pull/push/read_excel/mirror) at
  module level; Workspace + kwargs for overrides. Don't grow the surface.
- Roadmap (design doc): phase 2 pull/land/register + narwhals, phase 3 push
  with allowlist, phase 4 excel/csv, later oracle.
```

**Step 6: Full suite + commit**

Run: `uv run pytest` → all PASS.

```bash
git add -A
git commit -m "feat: migrate repo to wh package (wh.yaml, docs, remove POC)"
```

---

## Amendments (post-review, 2026-07-19)

Code review after execution found the plan's `build()` broke its own atomicity
invariant: phase 2 deleted the `.old` parquet backup before phases 3–4, so a
failure during the final `os.replace` (e.g. live db locked on Windows) left
the old db serving the new parquet files. **The implemented code deviates from
the plan here on purpose:** the old parquet dir is kept until the db swap
succeeds, and any failure after the swap rolls the parquet dir back. Locked in
by `test_failure_during_db_swap_restores_live_parquet`. Additional post-review
hardening: config read/parse errors wrapped in `ConfigError`, friendly
`freshness()` before first mirror, `_wh_prev` attach alias (a mirror named
`prev.duckdb` collided), compression validated at parse time, CLI mirror
wiring test.

## Done — phase 1 acceptance

- `import wh; wh.connect()` works from any subdir of a project with wh.yaml
- `uv run wh mirror` refreshes from the local SQL Server; `--only` keeps a complete mirror
- `wh.freshness()` reports per-table staleness
- Full pytest suite green without any server; integration tests green with `WH_TEST_DSN`

Phase 2 (pull/land/register + narwhals) gets its own plan.
