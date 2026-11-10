"""Alembic environment of the operator database: URL from the dashboard's
settings, metadata from api.dashboard.models (never api.models)."""

import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from api.dashboard.config import settings
from api.dashboard.models import Base

config = context.config
if config.config_file_name is not None:
    # As db/migrations/env.py: keep the loggers and the root level of a test run.
    _root_level = logging.getLogger().level
    fileConfig(config.config_file_name, disable_existing_loggers=False)
    logging.getLogger().setLevel(_root_level)

# Tests set a scratch database; otherwise OPERATOR_DATABASE_URL.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", settings.operator_database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, compare_type=True
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
