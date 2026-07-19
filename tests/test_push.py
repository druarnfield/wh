import pyarrow as pa
import pytest

from wh.errors import PushRefused, WhError
from wh.push import check_allowed, parse_target, sql_type


def test_parse_target():
    assert parse_target("Sandbox.dbo.results") == ("Sandbox", "dbo", "results")


def test_parse_target_requires_three_parts():
    for bad in ("results", "dbo.results", "a.b.c.d", "a..c"):
        with pytest.raises(WhError, match="Database.schema.table"):
            parse_target(bad)


def test_check_allowed_case_insensitive():
    check_allowed("SANDBOX", "DBO", ["Sandbox.dbo"])   # no raise


def test_check_allowed_refuses_and_names_allowlist():
    with pytest.raises(PushRefused, match=r"Sandbox\.dbo"):
        check_allowed("Prod", "dbo", ["Sandbox.dbo"])


def test_check_allowed_empty_allowlist_message():
    with pytest.raises(PushRefused, match="push.allow"):
        check_allowed("Sandbox", "dbo", [])


@pytest.mark.parametrize("arrow_type,expected", [
    (pa.int8(), "SMALLINT"),
    (pa.int16(), "SMALLINT"),
    (pa.int32(), "INT"),
    (pa.int64(), "BIGINT"),
    (pa.uint32(), "BIGINT"),
    (pa.float32(), "REAL"),
    (pa.float64(), "FLOAT"),
    (pa.bool_(), "BIT"),
    (pa.string(), "NVARCHAR(MAX)"),
    (pa.large_string(), "NVARCHAR(MAX)"),
    (pa.date32(), "DATE"),
    (pa.timestamp("us"), "DATETIME2"),
    (pa.time64("us"), "TIME"),
    (pa.decimal128(18, 4), "DECIMAL(18,4)"),
    (pa.binary(), "VARBINARY(MAX)"),
])
def test_sql_type_mapping(arrow_type, expected):
    assert sql_type(pa.field("c", arrow_type)) == expected


def test_sql_type_unsupported():
    with pytest.raises(WhError, match="list"):
        sql_type(pa.field("c", pa.list_(pa.int64())))
