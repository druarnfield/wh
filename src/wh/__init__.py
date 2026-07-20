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
from .errors import (
    ConfigError, PushRefused, SchemaMismatch, SemanticsError, SourceError, WhError,
)
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
    "SemanticsError",
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


def _optional_workspace() -> Workspace | None:
    """The default workspace, or None when no wh.yaml exists anywhere above
    cwd. A wh.yaml that exists but fails to PARSE still raises — a broken
    config must surface, not silently fall back to configless behaviour."""
    from .config import find_config

    try:
        find_config()
    except ConfigError:
        return None
    return workspace()


def read_excel(path, **kwargs):
    """Smart Excel reader. Works without a wh.yaml unless land= is given."""
    ws = _optional_workspace()
    if ws is not None:
        return ws.read_excel(path, **kwargs)
    if kwargs.pop("land", None) is not None:
        raise ConfigError("land= needs a wh.yaml workspace, and none was found")
    from .frames import default_backend, from_arrow
    from .sources.excel import read_excel_arrow

    backend = kwargs.pop("backend", None)
    return from_arrow(read_excel_arrow(path, **kwargs), backend or default_backend())


def read_csv(path, **kwargs):
    """DuckDB-sniffed CSV reader. Works without a wh.yaml unless land= is given."""
    ws = _optional_workspace()
    if ws is not None:
        return ws.read_csv(path, **kwargs)
    if kwargs.pop("land", None) is not None:
        raise ConfigError("land= needs a wh.yaml workspace, and none was found")
    import duckdb

    from .errors import WhError as _WhError
    from .frames import default_backend, from_arrow

    backend = kwargs.pop("backend", None)
    try:
        rel = duckdb.read_csv(str(path), **kwargs)
    except duckdb.Error as e:
        raise _WhError(f"could not read CSV {path}: {e}") from e
    return from_arrow(rel.to_arrow_table(), backend or default_backend())
