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
    "pull", "land", "register",
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


def register(frame, name):
    return workspace().register(frame, name)


def pull(sql, **kwargs):
    return workspace().pull(sql, **kwargs)


def land(sql, table, **kwargs):
    return workspace().land(sql, table, **kwargs)
