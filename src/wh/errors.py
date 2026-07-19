"""Exception family for wh. All library errors inherit WhError.

Messages should be notebook-friendly: one clear sentence plus the fix,
never a bare driver stack trace.
"""


class WhError(Exception):
    pass


class ConfigError(WhError):
    pass


class SourceError(WhError):
    pass


class PushRefused(WhError):
    pass


class SchemaMismatch(WhError):
    pass
