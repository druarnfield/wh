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
