"""Writeback to SQL Server: allowlist gate, type mapping, SQL builders.

The Arrow -> SQL Server type mapping (the contract for pushed tables):

    int8/int16      SMALLINT        date32/date64   DATE
    int32/uint8/16  INT             timestamp       DATETIME2
    int64/uint32+   BIGINT          timestamp[tz]   DATETIMEOFFSET
    float32         REAL            time32/time64   TIME
    float64         FLOAT           decimal(p,s)    DECIMAL(p,s)
    bool            BIT             binary          VARBINARY(MAX)
    string          NVARCHAR(MAX)   dictionary<T>   mapped as T (decoded)

Tz-aware timestamps MUST be DATETIMEOFFSET: the driver binds them with their
offset, and SQL Server would silently discard it converting to DATETIME2.
Anything else (lists, structs, ...) is refused with a WhError.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.types as pat

from .errors import PushRefused, SourceError, WhError


def parse_target(table: str) -> tuple[str, str, str]:
    parts = table.split(".")
    if len(parts) != 3 or not all(parts):
        raise WhError(
            f"push target must be 'Database.schema.table', got '{table}'"
        )
    return parts[0], parts[1], parts[2]


def check_allowed(database: str, schema: str, allow: list[str]) -> None:
    if not allow:
        raise PushRefused(
            "no push.allow entries in wh.yaml — add the schemas you may "
            "write to, e.g.  push:\n  allow: [Sandbox.dbo]"
        )
    key = f"{database}.{schema}".lower()
    if key not in {a.lower() for a in allow}:
        raise PushRefused(
            f"'{database}.{schema}' is not in push.allow "
            f"(allowed: {', '.join(allow)})"
        )


def quote(ident: str) -> str:
    return "[" + ident.replace("]", "]]") + "]"


def sql_type(field: pa.Field) -> str:
    t = field.type
    if pat.is_dictionary(t):
        return sql_type(pa.field(field.name, t.value_type))
    if pat.is_int8(t) or pat.is_int16(t):
        return "SMALLINT"
    if pat.is_int32(t) or pat.is_uint16(t) or pat.is_uint8(t):
        return "INT"
    if pat.is_int64(t) or pat.is_uint32(t) or pat.is_uint64(t):
        return "BIGINT"
    if pat.is_float32(t):
        return "REAL"
    if pat.is_float64(t):
        return "FLOAT"
    if pat.is_boolean(t):
        return "BIT"
    if pat.is_string(t) or pat.is_large_string(t) or pat.is_string_view(t):
        return "NVARCHAR(MAX)"
    if pat.is_date(t):
        return "DATE"
    if pat.is_timestamp(t):
        return "DATETIMEOFFSET" if t.tz is not None else "DATETIME2"
    if pat.is_time(t):
        return "TIME"
    if pat.is_decimal(t):
        return f"DECIMAL({t.precision},{t.scale})"
    if pat.is_binary(t) or pat.is_large_binary(t) or pat.is_binary_view(t):
        return "VARBINARY(MAX)"
    raise WhError(
        f"column '{field.name}': cannot push arrow type {t} to SQL Server"
    )


_EXISTS_SQL = (
    "SELECT count(*) FROM {db}.INFORMATION_SCHEMA.TABLES "
    "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND TABLE_TYPE = 'BASE TABLE'"
)


def _rollback_quietly(conn) -> None:
    """Rollback without masking the original error (the connection may
    already be dead, making rollback itself raise)."""
    try:
        conn.rollback()
    except Exception:
        pass


def push_arrow(
    conn,
    database: str,
    schema: str,
    name: str,
    table: pa.Table,
    *,
    if_exists: str = "fail",
    batch_size: int = 5_000,
) -> int:
    """Create (or replace) [database].[schema].[name] from an Arrow table.

    Runs entirely in one transaction on `conn` (any DB-API connection):
    commit on success, rollback on any failure."""
    if if_exists not in ("fail", "replace"):
        raise WhError(f"if_exists must be 'fail' or 'replace', got '{if_exists}'")

    qualified = f"{quote(database)}.{quote(schema)}.{quote(name)}"
    columns = ", ".join(f"{quote(f.name)} {sql_type(f)}" for f in table.schema)
    cursor = conn.cursor()
    try:
        cursor.execute(_EXISTS_SQL.format(db=quote(database)), [schema, name])
        (exists,) = cursor.fetchone()
        if exists:
            if if_exists == "fail":
                raise WhError(
                    f"{database}.{schema}.{name} already exists — "
                    f"pass if_exists='replace' to overwrite"
                )
            cursor.execute(f"DROP TABLE {qualified}")
        cursor.execute(f"CREATE TABLE {qualified} ({columns})")

        if table.num_rows:
            col_list = ", ".join(quote(f.name) for f in table.schema)
            placeholders = ", ".join("?" * table.num_columns)
            insert = f"INSERT INTO {qualified} ({col_list}) VALUES ({placeholders})"
            # materialises the whole table as Python rows (fine for
            # analysis-sized results); batch_size only bounds executemany calls
            cols = [c.to_pylist() for c in table.columns]
            rows = list(zip(*cols))
            for i in range(0, len(rows), batch_size):
                cursor.executemany(insert, rows[i : i + batch_size])
        conn.commit()
        return table.num_rows
    except WhError:
        _rollback_quietly(conn)
        raise
    except Exception as e:
        _rollback_quietly(conn)
        raise SourceError(
            f"push to {database}.{schema}.{name} failed: {e}"
        ) from e
    except BaseException:
        _rollback_quietly(conn)
        raise
    finally:
        cursor.close()
