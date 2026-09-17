"""
User ORM 模型与状态常量（Phase 3.4 Step 3 用户体系）。

职责：
    定义 users 表的 SQLAlchemy 2.0 ORM 映射 + UserStatus 状态常量集。

范围边界：
    - 仅数据层 ORM 映射；不含认证/加密逻辑（那是 core.security / services.auth 职责）。
    - 不定义 relationship（当前无外键关联表；documents.user_id 不建 DB 外键，
      保持现有 schema 最小改动）。

设计要点：
1) api_key_hash / token_hash 均为 UNIQUE NOT NULL：
   - api_key_hash = SHA-256(api_key) 十六进制（64 字符），用于 API Key 唯一识别与注册幂等；
   - token_hash   = SHA-256(opaque random token) 十六进制（64 字符），用于 Bearer 认证查询；
   - 明文 API Key / token 不落库，只存在于调用栈内存。
2) api_key_ciphertext / api_key_nonce：
   - AES-256-GCM 加密副本（密文 + tag 一起 base64 编码）与每条记录独立的 12B 随机 nonce
     （base64 编码存储）；
   - 仅服务器以 APP_MASTER_KEY 解密后注入 Embedding / LLM 业务链路。
3) status 双层默认（default + server_default），对齐 Document.status 风格；
   不使用 SQLAlchemy Enum 类型（避免 migration 痛点），用 String(16) + 应用层常量。
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import DateTime, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class UserStatus:
    """User 状态常量集（供 Repository / Service / API 共享校验）。"""

    ACTIVE: Final[str] = "ACTIVE"
    DISABLED: Final[str] = "DISABLED"

    # 全部合法状态集合
    ALL: Final[frozenset[str]] = frozenset({ACTIVE, DISABLED})


class User(Base):
    """users 表 ORM：用户身份 + API Key 加密存储 + token 哈希。"""

    __tablename__ = "users"

    # 主键
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # SHA-256(api_key) 十六进制（64 字符）：API Key 唯一识别 / 注册幂等；UNIQUE 禁止重复
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    # AES-256-GCM 密文 + tag（base64）：真实 API Key 的加密副本，明文不落库
    api_key_ciphertext: Mapped[str] = mapped_column(String(1024), nullable=False)

    # 每条记录独立的 12B 随机 nonce（base64）：AES-GCM 解密必需
    api_key_nonce: Mapped[str] = mapped_column(String(32), nullable=False)

    # SHA-256(opaque random token) 十六进制（64 字符）：Bearer 认证查询；UNIQUE 禁止重复
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    # 状态：ACTIVE / DISABLED（migration user 固定 DISABLED，不可用于真实业务）
    # Python 层 default + DB 层 server_default 双层默认；text("'ACTIVE'") 保证 MySQL DDL 正确
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=UserStatus.ACTIVE,
        server_default=text("'ACTIVE'"),
    )

    # 时间戳：DB 层 server_default=func.now()；updated_at 每次 UPDATE 刷新
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
