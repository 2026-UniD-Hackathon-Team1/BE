import os

from alembic import context
from backend.db import configured_engine
from backend.models import Base
from backend.settings import database_url

config = context.config
url = config.attributes.get("database_url") or os.environ.get("DATABASE_URL") or database_url()
if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = configured_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
