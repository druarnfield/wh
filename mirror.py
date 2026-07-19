#!/usr/bin/env python3
"""
mirror.py — mirror a curated SQL Server schema into a DuckDB file.

Extracts tables/queries from SQL Server via mssql-python's Arrow API and
loads them into a fresh .duckdb file, which is atomically swapped into
place on success. Two output modes per table:

  native  : data stored in DuckDB's native format (default for speed)
  parquet : data written as zstd parquet, exposed as a DuckDB view

Usage:
  python mirror.py --config mirror.yaml
  python mirror.py --config mirror.yaml --only referrals --only waitlist
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import yaml

DEFAULT_BATCH_SIZE = 100_000
VALID_MODES = {"native", "parquet"}


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


class ConfigError(Exception):
    pass


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
    dsn_env: str
    duckdb_path: Path
    parquet_dir: Path
    tables: list[TableSpec] = field(default_factory=list)


def load_config(path: Path) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")

    src = raw.get("source") or {}
    dst = raw.get("destination") or {}
    dfl = raw.get("defaults") or {}

    dsn_env = src.get("dsn_env")
    if not dsn_env:
        raise ConfigError(
            "source.dsn_env is required (env var holding the connection string)"
        )

    duckdb_path = dst.get("duckdb_path")
    if not duckdb_path:
        raise ConfigError("destination.duckdb_path is required")
    duckdb_path = Path(duckdb_path).resolve()
    parquet_dir = Path(dst.get("parquet_dir", duckdb_path.parent / "parquet")).resolve()

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
            raise ConfigError(
                f"table '{name}': mode must be one of {sorted(VALID_MODES)}"
            )

        extract = t.get("extract") or {}
        spec = TableSpec(
            name=name,
            source=source,
            schema_in_duckdb=t.get(
                "schema_in_duckdb", dfl.get("schema_in_duckdb", "main")
            ),
            description=t.get("description"),
            mode=mode,
            compression=t.get("compression", dfl.get("compression", "zstd")),
            compression_level=int(
                t.get("compression_level", dfl.get("compression_level", 9))
            ),
            batch_size=int(
                extract.get("batch_size", dfl.get("batch_size", DEFAULT_BATCH_SIZE))
            ),
        )
        key = (spec.schema_in_duckdb, spec.name)
        if key in seen:
            raise ConfigError(f"duplicate table {spec.schema_in_duckdb}.{spec.name}")
        seen.add(key)
        tables.append(spec)

    if not tables:
        raise ConfigError("no tables defined")

    return Config(
        dsn_env=dsn_env, duckdb_path=duckdb_path, parquet_dir=parquet_dir, tables=tables
    )


# --------------------------------------------------------------------------
# Extraction (mssql-python, Arrow API)
# --------------------------------------------------------------------------


def open_mssql(dsn_env: str):
    try:
        import mssql_python  # imported lazily so config errors surface without the driver
    except ImportError as e:
        raise ConfigError(
            "mssql-python is not installed (pip install mssql-python)"
        ) from e

    conn_str = os.environ.get(dsn_env)
    if not conn_str:
        raise ConfigError(f"environment variable {dsn_env} is not set")
    return mssql_python.connect(conn_str)


def arrow_source(cursor, spec: TableSpec):
    """Execute the extract and return an Arrow object DuckDB can scan.

    Prefers the streaming arrow_reader (constant memory); falls back to a
    fully materialised Arrow table on older driver versions.
    """
    cursor.execute(spec.source.sql())
    if hasattr(cursor, "arrow_reader"):
        return cursor.arrow_reader(batch_size=spec.batch_size)
    return cursor.arrow()


# --------------------------------------------------------------------------
# Load
# --------------------------------------------------------------------------


def sql_str(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def load_table(
    con: duckdb.DuckDBPyConnection,
    cursor,
    spec: TableSpec,
    staging_parquet_dir: Path,
    final_parquet_dir: Path,
) -> tuple[int, Path | None]:
    """Extract one table. Returns (row_count, final_parquet_path_or_None).

    Parquet-mode views are NOT created here: DuckDB binds read_parquet()
    at CREATE VIEW time, so the target file must already exist at its
    final path. Views are created after the parquet dir swap.
    """
    con.execute(f'CREATE SCHEMA IF NOT EXISTS "{spec.schema_in_duckdb}"')

    src = arrow_source(cursor, spec)
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

        # parquet mode
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


def create_view(
    con: duckdb.DuckDBPyConnection, spec: TableSpec, final_file: Path
) -> None:
    con.execute(
        f"CREATE VIEW {spec.qualified} AS "
        f"SELECT * FROM read_parquet({sql_str(str(final_file))})"
    )
    if spec.description:
        con.execute(f"COMMENT ON VIEW {spec.qualified} IS {sql_str(spec.description)}")


# --------------------------------------------------------------------------
# Build + atomic swap
# --------------------------------------------------------------------------


def run(cfg: Config, only: list[str] | None, keep_staging: bool) -> None:
    tables = cfg.tables
    if only:
        wanted = set(only)
        tables = [t for t in tables if t.name in wanted]
        missing = wanted - {t.name for t in tables}
        if missing:
            raise ConfigError(f"--only names not in config: {sorted(missing)}")
        print(
            f"NOTE: --only rebuilds a PARTIAL mirror ({len(tables)} tables); "
            f"the swapped-in file will not contain the other tables."
        )

    staging_root = cfg.duckdb_path.parent / ".mirror_staging"
    if staging_root.exists():
        shutil.rmtree(staging_root)
    staging_root.mkdir(parents=True)
    staging_db = staging_root / cfg.duckdb_path.name
    staging_parquet = staging_root / "parquet"

    any_parquet = any(t.mode == "parquet" for t in tables)

    mssql = open_mssql(cfg.dsn_env)
    con = duckdb.connect(str(staging_db))
    t0 = time.time()
    try:
        # ---- phase 1: extract everything into staging ----
        pending_views: list[tuple[TableSpec, Path]] = []
        for spec in tables:
            start = time.time()
            cursor = mssql.cursor()
            try:
                rows, final_file = load_table(
                    con, cursor, spec, staging_parquet, cfg.parquet_dir
                )
            finally:
                cursor.close()
            if final_file is not None:
                pending_views.append((spec, final_file))
            print(
                f"  {spec.schema_in_duckdb}.{spec.name:<30} "
                f"{spec.mode:<8} {rows:>12,} rows  {time.time() - start:6.1f}s"
            )

        # ---- phase 2: swap parquet dir into place ----
        # (must precede view creation: DuckDB binds read_parquet at
        # CREATE VIEW time, so files must exist at their final paths)
        if any_parquet:
            old = cfg.parquet_dir.with_name(cfg.parquet_dir.name + ".old")
            if old.exists():
                shutil.rmtree(old)
            if cfg.parquet_dir.exists():
                cfg.parquet_dir.rename(old)
            staging_parquet.rename(cfg.parquet_dir)
            if old.exists():
                shutil.rmtree(old)

        # ---- phase 3: create parquet-backed views ----
        for spec, final_file in pending_views:
            create_view(con, spec, final_file)
        con.close()
        con = None

        # ---- phase 4: swap the db file ----
        os.replace(staging_db, cfg.duckdb_path)
        print(f"Swapped into {cfg.duckdb_path}  (total {time.time() - t0:.1f}s)")
    finally:
        if con is not None:
            con.close()
        mssql.close()
        if staging_root.exists() and not keep_staging:
            shutil.rmtree(staging_root, ignore_errors=True)


def main() -> int:
    p = argparse.ArgumentParser(description="Mirror SQL Server tables into DuckDB")
    p.add_argument("--config", default="mirror.yaml", type=Path)
    p.add_argument(
        "--only",
        action="append",
        help="only rebuild these table names (partial mirror!)",
    )
    p.add_argument(
        "--keep-staging",
        action="store_true",
        help="keep staging dir on failure/success",
    )
    p.add_argument("--validate", action="store_true", help="parse config and exit")
    args = p.parse_args()

    try:
        cfg = load_config(args.config)
        if args.validate:
            print(f"OK: {len(cfg.tables)} tables -> {cfg.duckdb_path}")
            return 0
        run(cfg, args.only, args.keep_staging)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
