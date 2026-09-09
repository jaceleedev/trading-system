"""Explicit, application-specific configuration; never inherit another app's DB URL."""

import os
from dataclasses import dataclass

from sqlalchemy.engine import URL, make_url

LOCAL_DATABASE_URL = "postgresql+psycopg://trading:local-research-only@127.0.0.1:55432/trading"


@dataclass(frozen=True)
class Settings:
    database_url: URL

    @classmethod
    def from_env(cls) -> Settings:
        url = make_url(os.environ.get("TRADING_DATABASE_URL", LOCAL_DATABASE_URL))
        if url.drivername != "postgresql+psycopg":
            raise ValueError("TRADING_DATABASE_URL must use postgresql+psycopg")
        if not url.database:
            raise ValueError("A PostgreSQL database name is required")
        return cls(url)

    @property
    def safe_database_url(self) -> str:
        # Query parameters may also contain credentials, so omit all auth and query fields.
        return URL.create(
            self.database_url.drivername,
            host=self.database_url.host,
            port=self.database_url.port,
            database=self.database_url.database,
        ).render_as_string()
