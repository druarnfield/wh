"""Writeback to SQL Server: allowlist gate, type mapping, SQL builders.

The Arrow -> SQL Server type mapping (the contract for pushed tables):

    int8/int16      SMALLINT        date32/date64   DATE
    int32/uint8/16  INT             timestamp[*]    DATETIME2
    int64/uint32+   BIGINT          time32/time64   TIME
    float32         REAL            decimal(p,s)    DECIMAL(p,s)
    float64         FLOAT           binary          VARBINARY(MAX)
    bool            BIT             string          NVARCHAR(MAX)

Anything else (lists, structs, ...) is refused with a WhError.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.types as pat

from .errors import PushRefused, WhError


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
    if pat.is_string(t) or pat.is_large_string(t):
        return "NVARCHAR(MAX)"
    if pat.is_date(t):
        return "DATE"
    if pat.is_timestamp(t):
        return "DATETIME2"
    if pat.is_time(t):
        return "TIME"
    if pat.is_decimal(t):
        return f"DECIMAL({t.precision},{t.scale})"
    if pat.is_binary(t) or pat.is_large_binary(t):
        return "VARBINARY(MAX)"
    raise WhError(
        f"column '{field.name}': cannot push arrow type {t} to SQL Server"
    )
