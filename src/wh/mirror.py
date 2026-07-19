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
    extracted_at TIMESTAMP,  -- UTC
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
            rows, datetime.now(timezone.utc).replace(tzinfo=None),
            round(duration, 3), spec_hash(spec),
        ],
    )


def _create_view(con, spec: TableSpec, final_file: Path) -> None:
    con.execute(
        f"CREATE VIEW {spec.qualified} AS "
        f"SELECT * FROM read_parquet({sql_str(str(final_file))})"
    )
    if spec.description:
        con.execute(f"COMMENT ON VIEW {spec.qualified} IS {sql_str(spec.description)}")


def _prev_has(con, name: str, schema: str) -> bool:
    (n,) = con.execute(
        "SELECT count(*) FROM duckdb_tables() "
        "WHERE database_name = 'prev' AND schema_name = ? AND table_name = ?",
        [schema, name],
    ).fetchone()
    return n > 0


def build(
    cfg: Config,
    extract: Extract,
    *,
    only: list[str] | None = None,
    keep_staging: bool = False,
    log: Callable[[str], None] = print,
) -> None:
    tables = cfg.tables
    carried: list[TableSpec] = []
    if only:
        wanted = set(only)
        missing = wanted - {t.name for t in cfg.tables}
        if missing:
            raise ConfigError(f"--only names not in config: {sorted(missing)}")
        if cfg.duckdb_path.exists():
            tables = [t for t in cfg.tables if t.name in wanted]
            carried = [t for t in cfg.tables if t.name not in wanted]
        # no previous mirror: nothing to carry, fall through to a full build

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
        pending_views: list[tuple[TableSpec, Path]] = []

        # ---- phase 0: carry over unselected tables from the live mirror ----
        # Anything missing from the previous mirror is extracted fresh instead,
        # so the result is always a complete mirror.
        if carried:
            con.execute(f"ATTACH {sql_str(str(cfg.duckdb_path))} AS prev (READ_ONLY)")
            has_meta = _prev_has(con, "meta", "_mirror")
            for spec in carried:
                if spec.mode == "native":
                    if not _prev_has(con, spec.name, spec.schema_in_duckdb):
                        tables.append(spec)
                        continue
                    con.execute(f'CREATE SCHEMA IF NOT EXISTS "{spec.schema_in_duckdb}"')
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

        # ---- phase 1: extract into staging ----
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
        if staging_parquet.exists():
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
