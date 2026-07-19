"""Workspace — the hub everything hangs off."""

from __future__ import annotations

from pathlib import Path

import duckdb

from .config import Config, find_config, load_config
from .mirror import build as _build


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

    def freshness(self):
        """One row per mirrored table: mode, row_count, extracted_at, duration_s."""
        con = self.connect(read_only=True)
        try:
            return con.execute(
                "SELECT schema_name, table_name, mode, row_count, "
                "extracted_at, duration_s "
                "FROM _mirror.meta ORDER BY schema_name, table_name"
            ).to_arrow_table()
        finally:
            con.close()
