from alembic import context

from trading_research import models  # noqa: F401
from trading_research.database import Base, get_engine

with get_engine().connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
