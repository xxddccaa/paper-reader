from logging.config import fileConfig
from alembic import context
from app.db import Base, engine
from app import models
config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

def run_migrations_offline():
    from app.config import get_settings
    context.configure(url=get_settings().database_url, target_metadata=Base.metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    with engine().connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
