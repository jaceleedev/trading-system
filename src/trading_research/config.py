"""Explicit, application-specific configuration; never inherit another app's DB URL."""

import os
from dataclasses import dataclass

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

LOCAL_DATABASE_URL = "postgresql+psycopg://trading:local-research-only@127.0.0.1:55432/trading"
DATABASE_COMPONENTS = ("HOST", "PORT", "NAME", "USER", "PASSWORD")


def _url_from_text(value: str) -> URL:
    try:
        return make_url(value)
    except ArgumentError, TypeError, ValueError:
        # Parser messages can echo an invalid URL or port containing credentials.
        raise ValueError("Invalid TRADING_DATABASE_URL configuration") from None


def _url_from_components() -> URL:
    values = {name: os.environ.get(f"TRADING_DATABASE_{name}") for name in DATABASE_COMPONENTS}
    if any(not value for value in values.values()):
        raise ValueError(
            "TRADING_DATABASE_HOST, PORT, NAME, USER and PASSWORD must all be nonempty"
        )
    port_text = values["PORT"]
    if not port_text.isascii() or not port_text.isdecimal():
        raise ValueError("TRADING_DATABASE_PORT must be an integer from 1 through 65535")
    try:
        port = int(port_text)
    except ValueError:
        raise ValueError("TRADING_DATABASE_PORT must be an integer from 1 through 65535") from None
    if not 1 <= port <= 65535:
        raise ValueError("TRADING_DATABASE_PORT must be an integer from 1 through 65535")
    # Values are separate URL fields, not URL-escaped fragments; preserve passwords
    # including punctuation, Unicode and intentional leading/trailing whitespace.
    return URL.create(
        "postgresql+psycopg",
        username=values["USER"],
        password=values["PASSWORD"],
        host=values["HOST"],
        port=port,
        database=values["NAME"],
    )


@dataclass(frozen=True)
class Settings:
    database_url: URL

    @classmethod
    def from_env(cls) -> Settings:
        if "TRADING_DATABASE_URL" in os.environ:
            url = _url_from_text(os.environ["TRADING_DATABASE_URL"])
        elif any(f"TRADING_DATABASE_{name}" in os.environ for name in DATABASE_COMPONENTS):
            url = _url_from_components()
        else:
            url = _url_from_text(LOCAL_DATABASE_URL)
        if url.drivername != "postgresql+psycopg":
            raise ValueError("TRADING_DATABASE_URL must use postgresql+psycopg")
        if not url.database:
            raise ValueError("A PostgreSQL database name is required")
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError("PostgreSQL port must be from 1 through 65535")
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
