"""Workspace — the hub everything hangs off."""

from __future__ import annotations

from pathlib import Path

import duckdb

from .config import Config, find_config, load_config
from .errors import ConfigError, WhError
# NOTE: must be `from .mirror import ...` — `from . import mirror` returns the
# wh.mirror() FUNCTION defined in __init__.py, which shadows this module.
from .mirror import build as _build


class Workspace:
    def __init__(self, config: Config):
        self.config = config
        self._con: duckdb.DuckDBPyConnection | None = None

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Workspace":
        cfg_path = Path(path) if path is not None else find_config()
        return cls(load_config(cfg_path))

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

    def connect(
        self, *, fresh: bool = False, read_only: bool = False
    ) -> duckdb.DuckDBPyConnection:
        """The session connection — hand it to marimo/jupyter.

        fresh=True returns an independent read-write connection you own.
        read_only=True implies fresh; only useful from OTHER processes —
        it fails if this process already holds a write connection."""
        if read_only:
            return duckdb.connect(str(self.config.duckdb_path), read_only=True)
        if fresh:
            return duckdb.connect(str(self.config.duckdb_path))
        return self.con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

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
        # the swap replaces the db file; the old session handle would keep
        # serving the pre-refresh data, so drop it and reopen lazily
        self.close()
        if extract is None:
            from .sources.mssql import MssqlExtractor

            source = self.config.sources[self.config.default_source]
            extractor = MssqlExtractor(source)
            try:
                _build(
                    self.config, extractor, only=only,
                    keep_staging=keep_staging, log=log,
                )
            finally:
                extractor.close()
        else:
            _build(
                self.config, extract, only=only,
                keep_staging=keep_staging, log=log,
            )

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

    def register(self, frame, name: str) -> None:
        """Make any dataframe queryable (as `name`) on the session connection."""
        from .frames import to_arrow

        self.con.register(name, to_arrow(frame))

    def freshness(self):
        """One row per mirrored table: mode, row_count, extracted_at, duration_s."""
        if not self.config.duckdb_path.exists():
            raise WhError(
                f"no mirror at {self.config.duckdb_path} — run wh.mirror() first"
            )
        try:
            return self.con.execute(
                "SELECT schema_name, table_name, mode, row_count, "
                "extracted_at, duration_s "
                "FROM _mirror.meta ORDER BY schema_name, table_name"
            ).to_arrow_table()
        except duckdb.CatalogException as e:
            raise WhError(
                "this database has no mirror metadata — run wh.mirror() first"
            ) from e
