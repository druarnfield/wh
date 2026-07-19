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

from .cleaning import clean
from .errors import ConfigError, PushRefused, SchemaMismatch, SourceError, WhError
from .workspace import Workspace

# Initialise submodules whose names collide with the module-level verbs below
# (`push`, `mirror` via workspace, `workspace` itself). A submodule's FIRST
# import binds it onto the package — if that happens lazily after the verb
# functions are defined, it silently replaces the function with the module.
from . import push as _push_submodule  # noqa: F401  (side effect only)

__all__ = [
    "Workspace", "workspace", "connect", "mirror", "freshness",
    "pull", "land", "register", "push",
    "read_excel", "read_csv", "clean",
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


def push(frame, table, **kwargs):
    return workspace().push(frame, table, **kwargs)


def read_excel(path, **kwargs):
    """Smart Excel reader. Works without a wh.yaml unless land= is given."""
    try:
        ws = workspace()
    except ConfigError:
        if kwargs.get("land") is not None:
            raise
        from .frames import default_backend, from_arrow
        from .sources.excel import read_excel_arrow

        backend = kwargs.pop("backend", None)
        return from_arrow(
            read_excel_arrow(path, **kwargs), backend or default_backend()
        )
    return ws.read_excel(path, **kwargs)


def read_csv(path, **kwargs):
    """DuckDB-sniffed CSV reader. Works without a wh.yaml unless land= is given."""
    try:
        ws = workspace()
    except ConfigError:
        if kwargs.get("land") is not None:
            raise
        import duckdb

        from .frames import default_backend, from_arrow

        backend = kwargs.pop("backend", None)
        rel = duckdb.read_csv(str(path), **kwargs)
        return from_arrow(rel.to_arrow_table(), backend or default_backend())
    return ws.read_csv(path, **kwargs)
