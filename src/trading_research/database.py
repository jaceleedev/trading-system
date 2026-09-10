from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase

from trading_research.config import Settings


class Base(DeclarativeBase):
    pass


def get_engine(settings: Settings | None = None) -> Engine:
    config = settings or Settings.from_env()
    return create_engine(
        config.database_url,
        pool_pre_ping=True,
        pool_size=3,
        max_overflow=2,
        connect_args={"connect_timeout": 5},
    )
