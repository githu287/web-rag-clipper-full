"""
SQLAlchemy engine / session 基础设施（Phase 2.9 Step 1）。

职责：
    提供 MySQL engine（PyMySQL 驱动）与 sessionmaker 工厂，供 DocumentRepositoryImpl
    与 FastAPI Depends（get_db）使用。

设计要点（经验库 153832「配置单源 + 延迟真实连接」规则应用）：
1) 配置单源：engine URL 全部从 Settings 拼装，无硬编码 host/port/user/password。
2) 延迟真实连接：get_engine 用 lru_cache 单例，但仅在**首次调用**时 create_engine
   （create_engine 本身不开连接，真实连接在首次 SQL 执行时建立），保证「无 MySQL
   也能 import 本模块 / 构造对象」。
3) 密码转义：build_mysql_url 用 urllib.parse.quote_plus 转义密码特殊字符
   （@、:、/、# 等），避免含特殊字符的密码破坏 URL 解析。
4) expire_on_commit=False：sessionmaker 关闭 commit 后自动 expire，保证 Repository
   返回的 detached ORM 对象属性仍可读（当前 Document 无 relationship，安全）。
5) 不在 import 时建 engine（lru_cache 仅在调用时触发）。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Final
from urllib.parse import quote_plus
from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings


# MySQL 默认值（与 config.py 一致；此处仅作模块内文档性常量，真正单源仍是 Settings）
_DEFAULT_MYSQL_PORT: Final[int] = 3306
_DEFAULT_MYSQL_DATABASE: Final[str] = "rag_clipper"


def build_mysql_url(settings: Settings) -> str:
    """
    由 Settings 拼装 MySQL 连接 URL（PyMySQL 驱动，utf8mb4）。

    密码用 quote_plus 转义特殊字符（@、:、/、# 等），避免破坏 URL 解析。
    其余字段（user/host/port/database）按常规拼装。

    Args:
        settings: 配置单源实例。

    Returns:
        形如 `mysql+pymysql://user:pwd@host:port/database?charset=utf8mb4` 的 URL。
    """
    password = quote_plus(settings.mysql_password)
    return (
        f"mysql+pymysql://{settings.mysql_user}:{password}"
        f"@{settings.mysql_host}:{settings.mysql_port}"
        f"/{settings.mysql_database}?charset=utf8mb4"
    )


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """
    返回 MySQL Engine 单例（lru_cache 进程内唯一）。

    create_engine 本身不立即建立连接（SQLAlchemy 惰性），真实 TCP 连接在首次
    SQL 执行时建立，故 import 本模块 / 调用本函数不会因 MySQL 不可达而失败。

    Phase 2.10 Step 3.3 修复：此前签名 `get_engine(settings: Settings)` 在
    lru_cache 下不可用——pydantic BaseModel 默认不可 hash（定义了 __eq__ 后
    __hash__ 被置 None），真实调用即抛 `TypeError: unhashable type: 'Settings'`。
    现改为无参 + 内部取配置单源（延迟导入避免循环导入，与 get_db 一致）。

    Returns:
        Engine: 绑定 MySQL 的 SQLAlchemy Engine。
    """
    from .config import get_settings

    return create_engine(
        build_mysql_url(get_settings()),
        pool_pre_ping=True,
        pool_size=5,
        echo=False,
    )


@lru_cache(maxsize=1)
def get_session_factory(engine: Engine) -> sessionmaker[Session]:
    """
    返回绑定给定 engine 的 sessionmaker 单例。

    expire_on_commit=False：commit 后不自动 expire 对象属性，保证 Repository
    在 commit + refresh 后返回的 detached ORM 属性可读（当前 Document 无 relationship，
    安全；后续加 relationship 需 DTO 转换或 eager loading 避免 DetachedInstanceError）。

    Args:
        engine: SQLAlchemy Engine（通常来自 get_engine）。

    Returns:
        sessionmaker[Session]: 可调用工厂，调用返回新 Session。
    """
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """
    FastAPI Depends 依赖：每请求 yield 一个 Session，请求结束 close。

    本步骤（Phase 2.9 Step 1）仅定义，不接 router（无 HTTP 端点）。
    """
    factory = get_session_factory(get_engine())
    session = factory()
    try:
        yield session
    finally:
        session.close()
