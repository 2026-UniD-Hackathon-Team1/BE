"""Local SQLite connections; schema changes are owned by Alembic."""
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


def make_engine(url: str = "sqlite:///data/galatalkka.db"):
    if not url.startswith("sqlite:"):
        raise ValueError("Only local SQLite is supported")
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def local_engine():
    Path("data").mkdir(exist_ok=True)
    return make_engine()


def session_factory(engine):
    return sessionmaker(engine, expire_on_commit=False)
