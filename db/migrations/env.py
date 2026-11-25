"""Alembic-Umgebung. URL aus den App-Settings, Metadaten aus api.models."""

import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from api.config import settings
from api.models import Base

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers=False: sonst schaltet eine Migration im Test alle
    # bereits importierten App-Logger stumm (api.events.dispatcher, api.request).
    #
    # Root-Level danach zurücksetzen: db/alembic.ini setzt [logger_root] auf WARN,
    # fileConfig() wendet das unbedingt an. Läuft eine Migration im selben Prozess
    # wie die App (jeder Test über die migrated_db_url-Fixture), würde das
    # api.core.logging.configure_logging()s INFO-Pegel für den Rest des Prozesses
    # stumm überschreiben, ohne dass ein einzelner Test das lokal bemerkt.
    _root_level = logging.getLogger().level
    fileConfig(config.config_file_name, disable_existing_loggers=False)
    logging.getLogger().setLevel(_root_level)

# Tests setzen eine eigene URL (Wegwerf-DB); sonst gilt DATABASE_URL.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", settings.database_url)

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
