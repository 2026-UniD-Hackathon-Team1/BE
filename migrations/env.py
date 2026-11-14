from pathlib import Path

from alembic import context
from backend.db import make_engine
from backend.models import Base

config = context.config
url = config.get_main_option("sqlalchemy.url")
if url == "sqlite:///data/galatalkka.db":
    Path("data").mkdir(exist_ok=True)
if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = make_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
