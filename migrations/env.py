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
    connection = config.attributes.get("connection")
    if connection is not None:
        # 호출자가 잡은 트랜잭션과 잠금을 그대로 사용하고 연결 수명도 호출자에게 맡긴다.
        context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
    else:
        engine = configured_engine(url)
        try:
            with engine.connect() as connection:
                context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
                with context.begin_transaction():
                    context.run_migrations()
        finally:
            engine.dispose()
