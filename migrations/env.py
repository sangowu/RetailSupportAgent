"""Alembic 环境。

连接串**只从环境变量取**（`RSA_DATABASE_URL`），不写进 alembic.ini——
写进去迟早会带着口令进 Git。这条与项目里"密钥只从环境读"的规矩是同一条。
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
            "未设置 RSA_DATABASE_URL。迁移不接受写在配置文件里的连接串——"
            "那样口令迟早会进 Git。示例见 .env.example。"
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
