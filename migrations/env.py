"""Alembic environment.

The connection string is read only from the environment (`RSA_DATABASE_URL`), never from
alembic.ini: putting it there eventually carries a password into Git. This is the same rule
as "secrets are read only from the environment" elsewhere in the project.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from retail_support_agent.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url() -> str:
    url = os.getenv("RSA_DATABASE_URL")
    if not url:
        raise SystemExit(
            "RSA_DATABASE_URL is not set. Migrations do not accept a connection string "
            "written into a config file, because the password would end up in Git. "
            "See .env.example."
        )
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
