"""SQL Server extraction via mssql-python's Arrow API."""

from __future__ import annotations

from ..config import DEFAULT_BATCH_SIZE, Source, TableSpec
from ..errors import SourceError


class MssqlExtractor:
    """Implements the mirror extract seam: extractor(spec) -> Arrow.

    Keeps one connection; the cursor for a streaming arrow_reader must stay
    open while DuckDB consumes it, so it's closed on the next call / close().
    """

    def __init__(self, source: Source):
        try:
            import mssql_python
        except ImportError as e:
            raise SourceError(
                "mssql-python is not installed (uv add mssql-python)"
            ) from e
        try:
            self._conn = mssql_python.connect(source.connection_string())
        except SourceError:
            raise
        except Exception as e:
            raise SourceError(
                f"could not connect to source '{source.name}': {e}"
            ) from e
        self._cursor = None

    def query(self, sql: str, batch_size: int = DEFAULT_BATCH_SIZE):
        """Execute sql, return an Arrow reader (streaming) or table."""
        if self._cursor is not None:
            self._cursor.close()
            self._cursor = None
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql)
        except Exception as e:
            cursor.close()
            raise SourceError(f"query failed: {e}") from e
        self._cursor = cursor
        if hasattr(cursor, "arrow_reader"):
            return cursor.arrow_reader(batch_size=batch_size)
        return cursor.arrow()

    def __call__(self, spec: TableSpec):
        try:
            return self.query(spec.source.sql(), spec.batch_size)
        except SourceError as e:
            raise SourceError(f"extract failed for table '{spec.name}': {e}") from e

    def close(self) -> None:
        try:
            if self._cursor is not None:
                self._cursor.close()
        finally:
            self._conn.close()
