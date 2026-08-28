"""
PluginWorkspace ORM 模型与状态常量（Phase 3.5 Step 2-B 新增）。

Step 2-A migration 0007 已建 plugin_workspaces 表并回填历史数据；
本模块提供与之完全对齐的 SQLAlchemy 2.0 ORM 映射 + PluginStatus 状态常量集。

职责：
    仅数据层 ORM 映射；不含认证 / secret / API Key 加解密逻辑（那是 core.security /
    PluginService 职责）。

范围边界：
    - 不定义 relationship（对齐 document.py / user.py 的「无外键关联表」注释）；
    - documents.plugin_id 的 FK 在 migration 0007 已建（fk_documents_plugin_id_plugin_workspaces），
      本模型不声明 relationship，避免隐式查询副作用。

设计要点（Phase 3.5 双凭证身份体系）：
1) plugin_id（VARCHAR(64) UNIQUE NOT NULL）：Workspace 唯一标识，**非秘密**，
   对外可展示（剪藏来源、知识库命名空间）；
2) plugin_name（VARCHAR(64) NOT NULL）+ plugin_name_norm（VARCHAR(64) UNIQUE NOT NULL）：
   展示名 + 归一化名（strip → 连续空白压缩 → lower）；归一化名唯一保证「一名一 Workspace」；
3) plugin_secret_hash（CHAR(64) UNIQUE NOT NULL）：SHA-256(plugin_secret)，
   **数据库与日志绝不存明文 secret**；
4) api_key_ciphertext / api_key_nonce（**NULLABLE**）：百炼模型调用凭证的
   AES-256-GCM 密文 + nonce，**绝不参与身份识别**；NULL = 未配置；
5) status 双层默认（default + server_default），对齐 Document.status / User.status 风格；
   不用 SQLAlchemy Enum，用 String(16) + 应用层常量（PluginStatus）。

字段严格对齐 migration 0007：不加 migration 中不存在的字段
（无 username / password / token / plugin_secret 明文 / secret_rotation）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import BigInteger, DateTime, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class PluginStatus:
    """Plugin Workspace 状态常量集（供 Repository / Service / API 共享校验）。"""

    ACTIVE: Final[str] = "ACTIVE"
    DISABLED: Final[str] = "DISABLED"
    DELETING: Final[str] = "DELETING"

    # 全部合法状态集合
    ALL: Final[frozenset[str]] = frozenset({ACTIVE, DISABLED, DELETING})


class PluginWorkspace(Base):
    """plugin_workspaces 表 ORM：双凭证（plugin_id + plugin_secret）Workspace 身份。"""

    __tablename__ = "plugin_workspaces"

    # 主键（migration 0007：BIGINT PK；SQLite 验证环境以 Integer variant 保证自增）
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )

    # Workspace 唯一标识（非秘密；对外展示 / 归属过滤）
    plugin_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    # 展示名称与归一化名（归一化名唯一，保证「一名一 Workspace」）
    plugin_name: Mapped[str] = mapped_column(String(64), nullable=False)
    plugin_name_norm: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    # SHA-256(plugin_secret) 十六进制（64 字符）：认证凭证哈希；
    # UNIQUE 禁止哈希碰撞复用；明文 secret 不落库、不进日志
    plugin_secret_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    # 百炼模型调用凭证（不参与身份识别）：AES-256-GCM 密文 + tag（base64）；
    # NULL = 未配置 API Key
    api_key_ciphertext: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # 每条记录独立的 12B 随机 nonce（base64）：AES-GCM 解密必需；NULL = 未配置
    api_key_nonce: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # 状态：ACTIVE / DISABLED / DELETING；双层默认（default + server_default）
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=PluginStatus.ACTIVE,
        server_default=text("'ACTIVE'"),
    )

    # 时间戳：DB 层 server_default=func.now()；updated_at 每次 UPDATE 自动刷新
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
