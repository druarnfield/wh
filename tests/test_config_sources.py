import pytest

from wh.config import Auth, Source
from wh.errors import ConfigError


def make_source(**kw) -> Source:
    base = dict(
        name="warehouse",
        driver="mssql",
        server="localhost,1433",
        database="ExecReporting",
    )
    base.update(kw)
    return Source(**base)


def test_connection_string_user_password(monkeypatch):
    monkeypatch.setenv("WH_PWD", "s3cret")
    src = make_source(
        encrypt=False,
        trust_server_certificate=True,
        auth=Auth(user="sa", password_env="WH_PWD"),
    )
    assert src.connection_string() == (
        "Server=localhost,1433;Database=ExecReporting;"
        "UID=sa;PWD=s3cret;Encrypt=no;TrustServerCertificate=yes;"
    )


def test_connection_string_trusted():
    src = make_source(auth=Auth(trusted=True))
    assert src.connection_string() == (
        "Server=localhost,1433;Database=ExecReporting;"
        "Trusted_Connection=yes;Encrypt=yes;"
    )


def test_connection_string_dsn_env_escape_hatch(monkeypatch):
    monkeypatch.setenv("MY_DSN", "Server=x;Database=y;")
    src = Source(name="warehouse", driver="mssql", dsn_env="MY_DSN")
    assert src.connection_string() == "Server=x;Database=y;"


def test_dsn_env_unset_raises(monkeypatch):
    monkeypatch.delenv("MY_DSN", raising=False)
    src = Source(name="warehouse", driver="mssql", dsn_env="MY_DSN")
    with pytest.raises(ConfigError, match="MY_DSN"):
        src.connection_string()


def test_password_env_unset_raises(monkeypatch):
    monkeypatch.delenv("WH_PWD", raising=False)
    src = make_source(auth=Auth(user="sa", password_env="WH_PWD"))
    with pytest.raises(ConfigError, match="WH_PWD"):
        src.connection_string()


def test_missing_auth_raises():
    src = make_source()  # no auth at all
    with pytest.raises(ConfigError, match="auth"):
        src.connection_string()


def test_missing_server_raises():
    src = Source(name="warehouse", driver="mssql", database="db",
                 auth=Auth(trusted=True))
    with pytest.raises(ConfigError, match="server"):
        src.connection_string()
