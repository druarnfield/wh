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
# codecs DuckDB's parquet COPY accepts; checked at parse time so a typo
# fails `wh validate` instead of dying mid-build
VALID_COMPRESSION = {"uncompressed", "snappy", "gzip", "zstd", "brotli", "lz4", "lz4_raw"}
VALID_FRAMES = {"polars", "pandas", "pyarrow"}


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
    frames: str | None = None     # preferred pull() backend; None = auto
    push_allow: list[str] = field(default_factory=list)   # "Database.schema"
    semantics_dir: Path | None = None   # set by load_config; absent dir = no models
    fiscal_year_start: int = 1          # month FY starts (7 = July); folded into model hashes


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
    try:
        with open(path) as f:
            raw = yaml.safe_load(f)
    except OSError as e:
        raise ConfigError(f"cannot read config {path}: {e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e
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

    frames = dfl.get("frames")
    if frames is not None and frames not in VALID_FRAMES:
        raise ConfigError(f"defaults.frames must be one of {sorted(VALID_FRAMES)}")

    push_allow = list((raw.get("push") or {}).get("allow") or [])
    for entry in push_allow:
        parts = str(entry).split(".")
        if not isinstance(entry, str) or len(parts) != 2 or not all(parts):
            raise ConfigError(
                f"push.allow entry '{entry}' must be 'Database.schema'"
            )

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
        compression = str(t.get("compression", dfl.get("compression", "zstd"))).lower()
        if compression not in VALID_COMPRESSION:
            raise ConfigError(
                f"table '{name}': compression must be one of {sorted(VALID_COMPRESSION)}"
            )
        extract = t.get("extract") or {}
        spec = TableSpec(
            name=name,
            source=source,
            schema_in_duckdb=t.get("schema_in_duckdb", dfl.get("schema_in_duckdb", "main")),
            description=t.get("description"),
            mode=mode,
            compression=compression,
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
        frames=frames,
        push_allow=push_allow,
        semantics_dir=(
            base / ((raw.get("semantics") or {}).get("dir", "semantics"))
        ).resolve(),
        fiscal_year_start=_parse_fiscal_year_start(raw),
    )


def _parse_fiscal_year_start(raw: dict) -> int:
    v = (raw.get("semantics") or {}).get("fiscal_year_start", 1)
    if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 12:
        raise ConfigError(
            f"semantics.fiscal_year_start must be an integer 1-12 (got {v!r})"
        )
    return v


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
