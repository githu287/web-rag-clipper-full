"""
Milvus Repository 子包：
- Protocol 接口契约（MilvusRepository）
- MilvusInitializer：Phase 2.4 Step 2 幂等 Collection/Index 初始化
- PyMilvusRepositoryImpl：Phase 2.4 Step 3 MilvusRepository 的 pymilvus 实现
"""

# 重导出 Protocol，便于上层 Service 直接 `from repositories.milvus import MilvusRepository`
from .protocol import MilvusRepository as MilvusRepository

# 重导出 MilvusInitializer：启动期 / 管理命令入口可直接 `from repositories.milvus import MilvusInitializer`
from .initializer import MilvusInitializer as MilvusInitializer

# 重导出 PyMilvusRepositoryImpl：DI 工厂或启动期直接绑定 Protocol→Impl
from .impl import PyMilvusRepositoryImpl as PyMilvusRepositoryImpl

__all__ = ["MilvusRepository", "MilvusInitializer", "PyMilvusRepositoryImpl"]
