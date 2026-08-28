"""Workspace — the hub everything hangs off."""

from __future__ import annotations

from pathlib import Path

import duckdb

from .config import Config, find_config, load_config
from .errors import ConfigError, SourceError, WhError
# NOTE: must be `from .mirror import ...` — `from . import mirror` returns the
# wh.mirror() FUNCTION defined in __init__.py, which shadows this module.
from .mirror import build as _build


def _split_table(table: str) -> tuple[str, str]:
    parts = table.split(".")
    if len(parts) == 1:
        schema, name = "main", parts[0]
    elif len(parts) == 2:
        schema, name = parts
    else:
        raise WhError(f"table must be 'name' or 'schema.name', got '{table}'")
    if not schema or not name:
        raise WhError(f"table must be 'name' or 'schema.name', got '{table}'")
    if schema == "_mirror":
        raise WhError("the _mirror schema holds mirror metadata — land elsewhere")
    return schema, name


def _qi(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


class Workspace:
    def __init__(self, config: Config):
        self.config = config
        self._con: duckdb.DuckDBPyConnection | None = None
        self._metrics_cache: tuple | None = None    # (yaml mtimes key, models)
        self._metrics_checked: tuple | None = None  # (con, defs key, {model: warnings})

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
        from .frames import VALID_BACKENDS, default_backend, from_arrow, to_arrow
        from .sources.mssql import MssqlExtractor

        if backend is not None and backend not in VALID_BACKENDS:
            raise WhError(
                f"unknown frames backend '{backend}' (use one of {VALID_BACKENDS})"
            )
        extractor = MssqlExtractor(self._source(source))
        try:
            # read_all() inside to_arrow drains a streaming reader BEFORE
            # the finally-close below kills its cursor. Keep it that way.
            table = to_arrow(extractor.query(sql))
        finally:
            extractor.close()
        return from_arrow(table, backend or self.config.frames or default_backend())

    def land(self, sql: str, table: str, *, source: str | None = None) -> int:
        """Stream sql results straight into the local DuckDB (constant memory).

        Re-landing the same table replaces it. Returns the row count.

        Landed tables are session scratch: `wh.mirror()` rebuilds the database
        from the config alone, so a refresh WIPES anything you landed."""
        from .sources.mssql import MssqlExtractor

        schema, name = _split_table(table)
        qualified = f"{_qi(schema)}.{_qi(name)}"

        con = self.con
        extractor = MssqlExtractor(self._source(source))
        try:
            con.register("_wh_land_src", extractor.query(sql))
            try:
                con.execute(f"CREATE SCHEMA IF NOT EXISTS {_qi(schema)}")
                try:
                    con.execute(
                        f"CREATE OR REPLACE TABLE {qualified} "
                        f"AS SELECT * FROM _wh_land_src"
                    )
                except duckdb.Error as e:
                    # a mid-stream source failure surfaces here, via DuckDB
                    raise SourceError(f"landing '{table}' failed: {e}") from e
            finally:
                con.unregister("_wh_land_src")
        finally:
            extractor.close()
        (count,) = con.execute(f"SELECT count(*) FROM {qualified}").fetchone()
        return count

    def push(
        self,
        frame,
        table: str,
        *,
        source: str | None = None,
        if_exists: str = "fail",
    ) -> int:
        """Publish a dataframe/relation to SQL Server. Allowlist-gated.

        `table` is 'Database.schema.table'. if_exists: 'fail' | 'replace'."""
        from .frames import to_arrow
        from .push import check_allowed, parse_target, push_arrow
        from .sources.mssql import open_connection

        database, schema, name = parse_target(table)
        check_allowed(database, schema, self.config.push_allow)
        arrow = to_arrow(frame)
        conn = open_connection(self._source(source))
        try:
            return push_arrow(
                conn, database, schema, name, arrow, if_exists=if_exists
            )
        finally:
            conn.close()

    def register(self, frame, name: str) -> None:
        """Make any dataframe queryable (as `name`) on the session connection."""
        from .frames import to_arrow

        self.con.register(name, to_arrow(frame))

    def _metric_models(self, reload: bool = False) -> dict:
        """Loaded metric models. Self-keying cache on YAML paths + mtimes —
        editing a file reloads automatically; no invalidation hooks."""
        from .metrics.loader import load_definitions

        d = self.config.semantics_dir
        if d is None or not d.is_dir():
            key = None
        else:
            key = tuple(sorted(
                (str(p), p.stat().st_mtime)
                for p in [*d.glob("*.yml"), *d.glob("*.yaml")]
            ))
        if reload or self._metrics_cache is None or self._metrics_cache[0] != key:
            loaded = (
                load_definitions(d, self.config.fiscal_year_start) if key else {}
            )
            self._metrics_cache = (key, loaded)
        return self._metrics_cache[1]

    def model(self, name: str, reload: bool = False):
        """One metric model, bound to the mirror (bind checks run once per
        connection). Slicing hangs off it: ws.model('x').slice(...)."""
        from .errors import SemanticsError
        from .metrics.result import BoundModel

        models = self._metric_models(reload)
        if name not in models:
            available = ", ".join(sorted(models)) or (
                f"none — add YAML files to {self.config.semantics_dir}"
            )
            raise SemanticsError(f"no metric model '{name}' (available: {available})")
        return BoundModel(models[name], self)

    def slice(self, model_name: str, **kwargs):
        return self.model(model_name).slice(**kwargs)

    def _bind_warnings(self, model) -> list:
        """Bind-check cache, self-keying on connection identity AND the loaded
        definitions (YAML mtimes) — an mtime reload hands out new Models,
        which must be re-checked, not trusted under a stale name key."""
        from .metrics.checks import bind_checks

        con = self.con
        defs_key = self._metrics_cache[0] if self._metrics_cache else None
        if (
            self._metrics_checked is None
            or self._metrics_checked[0] is not con
            or self._metrics_checked[1] != defs_key
        ):
            self._metrics_checked = (con, defs_key, {})
        cache = self._metrics_checked[2]
        if model.name not in cache:
            cache[model.name] = bind_checks(con, model)
        return cache[model.name]

    def _backend(self, backend: str | None) -> str:
        from .frames import default_backend

        return backend or self.config.frames or default_backend()

    def _land_obj(self, obj, table: str) -> int:
        """CREATE OR REPLACE a workspace table from an Arrow table/relation."""
        schema, name = _split_table(table)
        qualified = f"{_qi(schema)}.{_qi(name)}"
        con = self.con
        con.register("_wh_file_src", obj)
        try:
            con.execute(f"CREATE SCHEMA IF NOT EXISTS {_qi(schema)}")
            try:
                con.execute(
                    f"CREATE OR REPLACE TABLE {qualified} AS SELECT * FROM _wh_file_src"
                )
            except duckdb.Error as e:
                raise WhError(f"landing '{table}' failed: {e}") from e
        finally:
            con.unregister("_wh_file_src")
        (count,) = con.execute(f"SELECT count(*) FROM {qualified}").fetchone()
        return count

    def read_excel(
        self,
        path,
        *,
        sheet: str | int | None = None,
        header="auto",
        skip_rows: int = 0,
        backend: str | None = None,
        land: str | None = None,
    ):
        """Smart Excel reader (see wh.sources.excel). Returns a frame, or the
        row count when land='schema.table' writes it into the workspace db."""
        from .frames import from_arrow
        from .sources.excel import read_excel_arrow

        table = read_excel_arrow(path, sheet=sheet, header=header, skip_rows=skip_rows)
        if land is not None:
            return self._land_obj(table, land)
        return from_arrow(table, self._backend(backend))

    def read_csv(
        self,
        path,
        *,
        backend: str | None = None,
        land: str | None = None,
        **options,
    ):
        """CSV via DuckDB's sniffing reader; **options pass to read_csv."""
        from .frames import from_arrow
        from pathlib import Path as _Path

        if not _Path(path).exists():
            raise WhError(f"no such file: {path}")
        if land is not None:
            rel = self.con.read_csv(str(path), **options)
            return self._land_obj(rel, land)
        # frame-only path sniffs on an in-memory connection so a mere CSV
        # read never creates/locks the workspace .duckdb file
        try:
            rel = duckdb.read_csv(str(path), **options)
        except duckdb.Error as e:
            raise WhError(f"could not read CSV {path}: {e}") from e
        return from_arrow(rel.to_arrow_table(), self._backend(backend))

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
