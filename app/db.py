from sqlalchemy import Engine, create_engine, event, inspect

from .models import Base, Invoice, Order, OrderItem

# Raise this when a table's layout changes.
SCHEMA = 3


def make_engine(url: str) -> Engine:
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 15})

    @event.listens_for(engine, "connect")
    def _configure(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


class OldDatabase(Exception):
    pass


def create_tables(engine: Engine) -> None:
    """Create what's missing. Order tables with an older layout are rebuilt if empty."""
    with engine.begin() as db:
        version = db.exec_driver_sql("PRAGMA user_version").scalar()
        if version != SCHEMA and inspect(db).has_table("orders"):
            if db.exec_driver_sql("SELECT count(*) FROM orders").scalar():
                raise OldDatabase(
                    f"{engine.url.database} has orders from an earlier version of this app. "
                    "Move it away and start again, or delete its orders with: "
                    "uv run python -m app.seed --reset"
                )
            old = [model.__table__ for model in (Invoice, OrderItem, Order)]
            Base.metadata.drop_all(db, tables=old)
        Base.metadata.create_all(db)
        db.exec_driver_sql(f"PRAGMA user_version = {SCHEMA}")
