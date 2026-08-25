"""
SQLAlchemy 2.0 ORM declarative Base（Phase 2.9 Step 1）。

职责：
    提供所有 ORM 模型共享的 DeclarativeBase，作为 Alembic target_metadata 的唯一来源。

设计要点：
1) 采用 SQLAlchemy 2.0 风格 DeclarativeBase（而非 legacy declarative_base()），与项目
   锁定的 SQLAlchemy 2.0.52 对齐，获得 Mapped[]/mapped_column 类型推导能力。
2) Base 仅声明一次，所有 ORM 模型（当前仅 Document，后续 Page 等）继承它；
   Alembic env.py 的 target_metadata = Base.metadata 即可覆盖全部 ORM。
3) 本模块不建 engine / session（那是 core.db 职责），不 import 任何 ORM 子模块
   （避免 Base 与模型之间的循环导入；模型在各自模块 import Base）。
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的声明基类。"""
    pass
