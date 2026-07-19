from wh.errors import WhError, ConfigError, SourceError, PushRefused, SchemaMismatch


def test_all_errors_inherit_wherror():
    for exc in (ConfigError, SourceError, PushRefused, SchemaMismatch):
        assert issubclass(exc, WhError)
    assert issubclass(WhError, Exception)
