"""
models 包：跨层共享的 Pydantic Schema / DTO / SQLAlchemy ORM models。

子模块：
- api_schema  : Pydantic API 请求/响应 schema
- milvus_dto : Pydantic Milvus 数据契约（ChunkVector / ChunkSearchResult）
- base       : SQLAlchemy 2.0 declarative Base（Phase 2.9 Step 1）
- document   : Document ORM + DocumentStatus（Phase 2.9 Step 1）

注意：
- 显式 `from . import document` 触发 Document ORM 注册到 Base.metadata，
  确保 Alembic env.py（`import backend.models.document`）加载时 metadata 已就位。
"""

from . import document  # noqa: F401  确保 Alembic 加载时 Document ORM 注册到 Base.metadata
from .document import Document  # noqa: F401

__all__ = ["document", "Document"]
