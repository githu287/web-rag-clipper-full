"""
Alembic migration 环境入口（Phase 2.9 Step 1）。

职责：
1) 加载 backend.models.base.Base.metadata 作为 target_metadata；
2) 触发 backend.models.document import，确保 Document ORM 已注册到 metadata；
3) 从 Settings 拼装 MySQL URL 注入 alembic 配置；
4) 暴露 ALEMBIC_DATABASE_URL 环境变量覆盖入口，便于测试用 SQLite 验证。

设计要点：
- 使用 RuntimeError 而非 assert 做注册检查（避免 -O 模式失效）；
- 使用 `import backend.models.document` 而非 `from backend.models import document`，
  避免 models 包 __init__.py 触发其他子模块的循环导入风险；
- 不在 env.py 内创建 engine 之外的对象（避免 import 时连接 MySQL）。
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# 触发 Document ORM 注册到 Base.metadata；不使用 `from backend.models import document`
# 避免 models/__init__.py 在 alembic 环境下引发潜在循环导入。
from backend.core.config import get_settings
from backend.core.db import build_mysql_url
from backend.models.base import Base
import backend.models.document  # noqa: F401

# alembic 配置实例
config = context.config

# 日志配置
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------- ORM 注册检查
# 不使用 assert：Python -O 模式下 assert 会被剥离，导致检查失效。
if "documents" not in Base.metadata.tables:
    raise RuntimeError(
        "Document ORM 未注册，请检查 backend.models.document import"
    )

target_metadata = Base.metadata

# ------------------------------------------------------------------- URL 注入
# 优先环境变量覆盖（测试用 SQLite），否则从 Settings 拼装 MySQL URL。
# 这样 alembic.ini 的 sqlalchemy.url 留空，避免硬编码连接串。
url_override = os.environ.get("ALEMBIC_DATABASE_URL")
if url_override:
    config.set_main_option("sqlalchemy.url", url_override)
else:
    settings = get_settings()
    config.set_main_option("sqlalchemy.url", build_mysql_url(settings))


def run_migrations_offline() -> None:
    """
    离线模式：仅生成 SQL，不连接 DB。
    用于 `alembic upgrade head --sql` 等场景。
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    在线模式：连接 DB 执行 migration。
    用 NullPool 避免连接池在 migration 进程退出时遗留连接。
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
