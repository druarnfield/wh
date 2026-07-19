"""wh.yaml loading, validation, and discovery."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .errors import ConfigError

DEFAULT_BATCH_SIZE = 100_000
VALID_MODES = {"native", "parquet"}
VALID_DRIVERS = {"mssql"}


@dataclass
class Auth:
    user: str | None = None
    password_env: str | None = None
    trusted: bool = False


@dataclass
class Source:
    name: str
    driver: str
    server: str | None = None
    database: str | None = None
    encrypt: bool = True
    trust_server_certificate: bool = False
    auth: Auth = field(default_factory=Auth)
    dsn_env: str | None = None

    def connection_string(self) -> str:
        """Assemble the driver connection string. Secrets come from env vars
        at call time, so a config can be parsed without them set."""
        if self.dsn_env:
            dsn = os.environ.get(self.dsn_env)
            if not dsn:
                raise ConfigError(
                    f"source '{self.name}': env var {self.dsn_env} is not set"
                )
            return dsn
        if not self.server or not self.database:
            raise ConfigError(
                f"source '{self.name}': needs server and database (or dsn_env)"
            )
        parts = [f"Server={self.server}", f"Database={self.database}"]
        if self.auth.trusted:
            parts.append("Trusted_Connection=yes")
        elif self.auth.user and self.auth.password_env:
            pwd = os.environ.get(self.auth.password_env)
            if not pwd:
                raise ConfigError(
                    f"source '{self.name}': env var {self.auth.password_env} is not set"
                )
            parts += [f"UID={self.auth.user}", f"PWD={pwd}"]
        else:
            raise ConfigError(
                f"source '{self.name}': auth needs 'trusted: true' or "
                f"'user' + 'password_env'"
            )
        parts.append(f"Encrypt={'yes' if self.encrypt else 'no'}")
        if self.trust_server_certificate:
            parts.append("TrustServerCertificate=yes")
        return ";".join(parts) + ";"
