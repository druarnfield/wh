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


def test_sql_type_tz_aware_timestamp_keeps_offset():
    # driver binds tz-aware datetimes as DATETIMEOFFSET; DATETIME2 would
    # silently discard the offset server-side
    assert sql_type(pa.field("c", pa.timestamp("us", tz="UTC"))) == "DATETIMEOFFSET"
    assert sql_type(pa.field("c", pa.timestamp("us"))) == "DATETIME2"


def test_sql_type_dictionary_recurses_to_value_type():
    # polars Categorical arrives dictionary-encoded; to_pylist() decodes it
    t = pa.dictionary(pa.int32(), pa.large_string())
    assert sql_type(pa.field("c", t)) == "NVARCHAR(MAX)"


class FakeCursor:
    def __init__(self, table_exists):
        self.table_exists = table_exists
        self.executed: list[str] = []
        self.many: list[tuple[str, list]] = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        return self

    def fetchone(self):
        return (1 if self.table_exists else 0,)

    def executemany(self, sql, rows):
        self.many.append((sql, list(rows)))

    def close(self):
        pass


class FakeConn:
    def __init__(self, table_exists=False):
        self.cur = FakeCursor(table_exists)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


TABLE = pa.table({"n": [1, 2], "s": ["a", None]})


def test_push_arrow_creates_and_inserts():
    from wh.push import push_arrow

    conn = FakeConn(table_exists=False)
    assert push_arrow(conn, "Sandbox", "dbo", "res", TABLE, if_exists="fail") == 2
    create = next(s for s in conn.cur.executed if s.startswith("CREATE TABLE"))
    assert create == (
        "CREATE TABLE [Sandbox].[dbo].[res] ([n] BIGINT, [s] NVARCHAR(MAX))"
    )
    insert_sql, rows = conn.cur.many[0]
    assert insert_sql == "INSERT INTO [Sandbox].[dbo].[res] ([n], [s]) VALUES (?, ?)"
    assert rows == [(1, "a"), (2, None)]
    assert conn.commits == 1


def test_push_arrow_fail_when_exists():
    from wh.push import push_arrow

    conn = FakeConn(table_exists=True)
    with pytest.raises(WhError, match="if_exists='replace'"):
        push_arrow(conn, "Sandbox", "dbo", "res", TABLE, if_exists="fail")
    assert conn.rollbacks == 1


def test_push_arrow_replace_drops_first():
    from wh.push import push_arrow

    conn = FakeConn(table_exists=True)
    push_arrow(conn, "Sandbox", "dbo", "res", TABLE, if_exists="replace")
    assert "DROP TABLE [Sandbox].[dbo].[res]" in conn.cur.executed
    assert conn.commits == 1


def test_push_arrow_bad_if_exists():
    from wh.push import push_arrow

    with pytest.raises(WhError, match="if_exists"):
        push_arrow(FakeConn(), "Sandbox", "dbo", "res", TABLE, if_exists="append")


def test_push_arrow_empty_table_creates_no_insert():
    from wh.push import push_arrow

    conn = FakeConn(table_exists=False)
    empty = TABLE.slice(0, 0)
    assert push_arrow(conn, "Sandbox", "dbo", "res", empty, if_exists="fail") == 0
    assert conn.cur.many == []
    assert conn.commits == 1


def test_workspace_push_checks_allowlist_before_connecting(project, monkeypatch):
    import polars as pl
    import wh.sources.mssql as mssql_mod
    from wh.workspace import Workspace

    def no_connect(source):
        raise AssertionError("must refuse before opening a connection")

    monkeypatch.setattr(mssql_mod, "open_connection", no_connect)
    ws = Workspace.load(project / "wh.yaml")   # project fixture has no push.allow
    with pytest.raises(PushRefused, match="push.allow"):
        ws.push(pl.DataFrame({"a": [1]}), "Sandbox.dbo.res")


def test_workspace_push_happy_path(project, monkeypatch):
    import polars as pl
    import yaml
    import wh.sources.mssql as mssql_mod
    from wh.workspace import Workspace

    cfg = yaml.safe_load((project / "wh.yaml").read_text())
    cfg["push"] = {"allow": ["Sandbox.dbo"]}
    (project / "wh.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))

    conn = FakeConn(table_exists=False)
    monkeypatch.setattr(mssql_mod, "open_connection", lambda source: conn)
    ws = Workspace.load(project / "wh.yaml")
    assert ws.push(pl.DataFrame({"a": [1, 2, 3]}), "Sandbox.dbo.res") == 3
    assert conn.commits == 1


def test_push_arrow_quotes_hostile_identifiers():
    from wh.push import push_arrow

    conn = FakeConn(table_exists=False)
    tbl = pa.table({"we]ird": [1]})
    push_arrow(conn, "Sand]box", "dbo", "res", tbl)
    create = next(s for s in conn.cur.executed if s.startswith("CREATE TABLE"))
    assert create == "CREATE TABLE [Sand]]box].[dbo].[res] ([we]]ird] BIGINT)"


def test_push_arrow_exists_check_ignores_views():
    from wh.push import push_arrow

    conn = FakeConn(table_exists=False)
    push_arrow(conn, "Sandbox", "dbo", "res", TABLE)
    assert "TABLE_TYPE = 'BASE TABLE'" in conn.cur.executed[0]


def test_push_arrow_batches_inserts():
    from wh.push import push_arrow

    conn = FakeConn(table_exists=False)
    tbl = pa.table({"n": list(range(7))})
    push_arrow(conn, "Sandbox", "dbo", "res", tbl, batch_size=3)
    assert [len(rows) for _, rows in conn.cur.many] == [3, 3, 1]


def test_module_verbs_survive_submodule_imports():
    # a submodule's FIRST initialisation binds it onto the package, clobbering
    # any same-named function from __init__ — so those submodules must be
    # initialised eagerly during `import wh`, before the verbs are defined
    import importlib

    import wh

    for mod in ("wh.push", "wh.mirror", "wh.workspace"):
        importlib.import_module(mod)
    assert callable(wh.push)
    assert callable(wh.mirror)
    assert callable(wh.workspace)


def test_push_arrow_rolls_back_and_wraps_driver_errors():
    from wh.errors import SourceError
    from wh.push import push_arrow

    conn = FakeConn(table_exists=False)

    def explode(sql, rows):
        raise RuntimeError("bulk load failed")

    conn.cur.executemany = explode
    with pytest.raises(SourceError, match="bulk load failed"):
        push_arrow(conn, "Sandbox", "dbo", "res", TABLE, if_exists="fail")
    assert conn.rollbacks == 1
    assert conn.commits == 0


def test_push_arrow_failed_rollback_keeps_original_error():
    from wh.errors import SourceError
    from wh.push import push_arrow

    conn = FakeConn(table_exists=False)

    def explode(sql, rows):
        raise RuntimeError("connection lost")

    def rollback_also_dead():
        raise RuntimeError("rollback failed too")

    conn.cur.executemany = explode
    conn.rollback = rollback_also_dead
    with pytest.raises(SourceError, match="connection lost"):
        push_arrow(conn, "Sandbox", "dbo", "res", TABLE)
