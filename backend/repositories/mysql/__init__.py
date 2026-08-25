"""
MySQL Repository 子包（Phase 2.9 Step 1；Phase 3.4 Step 3 新增 User 族）：
- Document 族：DocumentRepository（Protocol） + DocumentRepositoryImpl（SQLAlchemy 实现）
- User 族    ：UserRepository（Protocol） + UserRepositoryImpl（SQLAlchemy 实现）

依赖：core.db.get_engine 提供 MySQL Engine 单例；core.config.Settings 提供
连接参数；models.document / models.user 提供 ORM。本包不直接持有 engine /
sessionmaker 单例，由 core.di 工厂装配。
"""

# 重导出 Protocol，便于上层 Service 直接 `from repositories.mysql import DocumentRepository`
from .protocol import DocumentRepository as DocumentRepository

# 重导出 DocumentRepositoryImpl：DI 工厂或启动期直接绑定 Protocol→Impl
from .impl import DocumentRepositoryImpl as DocumentRepositoryImpl

# 重导出 User 族 Protocol + Impl（Phase 3.4 Step 3 新增）
from .user_protocol import UserRepository as UserRepository
from .user_impl import UserRepositoryImpl as UserRepositoryImpl

__all__ = [
    "DocumentRepository",
    "DocumentRepositoryImpl",
    "UserRepository",
    "UserRepositoryImpl",
]
