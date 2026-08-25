"""
core 包：配置、全局异常、依赖注入工厂等跨层共享基础设施。

本阶段（Phase 2.9 Step 1）暴露：
- config.Settings / get_default_settings / get_settings（Pydantic 配置单源及别名）
- db.build_mysql_url / get_engine / get_session_factory / get_db（MySQL engine/session 基础设施）
- exceptions.MilvusRepositoryError 族（Milvus Repository 异常契约）
- exceptions.DocumentRepositoryError 族（Document Repository 异常契约，Phase 2.9 Step 1 新增）
- di.get_settings / get_milvus_repository / get_milvus_initializer / get_embedding_client
       / get_ingest_service / get_rag_service（依赖注入工厂）
"""
