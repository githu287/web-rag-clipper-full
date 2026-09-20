# Web RAG Clipper 文件索引

本索引按职责说明当前仓库文件。运行时生成的 `.env`、`.venv/`、`uploads/`、缓存和 evaluation 私有输出均被 Git 忽略，不属于源代码。

## 根目录

| 文件 | 说明 |
|---|---|
| `README.md` | 项目能力、安装、运行、API、测试与限制 |
| `ARCHITECTURE.md` | 当前实现的分层、数据流、隔离与一致性设计 |
| `FILE_INDEX.md` | 本文件 |
| `.env.example` | 环境变量模板；部分字段是尚未消费的预留配置 |
| `.gitignore` | Python、IDE、运行数据、扩展构建与评测私有产物忽略规则 |
| `docker-compose.yml` | MySQL、Redis、etcd、MinIO、Milvus standalone 编排 |
| `alembic.ini` | Alembic 配置入口；数据库 URL 由运行时环境注入 |

## `backend/` — FastAPI 后端

### 应用与 API

| 文件 | 说明 |
|---|---|
| `backend/main.py` | FastAPI 工厂、模块级 `app`、Milvus lifespan、Router 与全局异常处理器 |
| `backend/api/__init__.py` | API 包标记 |
| `backend/api/deps.py` | 从双请求头解析当前 Workspace |
| `backend/api/routers/__init__.py` | Router 子包标记；文件内路由清单注释已落后 |
| `backend/api/routers/plugins.py` | Workspace 注册、详情、改名、API Key 与删除 |
| `backend/api/routers/documents.py` | 文档列表/详情、创建、上传、ingest 与删除 |
| `backend/api/routers/clips.py` | 网页正文剪藏 |
| `backend/api/routers/jobs.py` | 异步入库任务查询与失败重试 |
| `backend/api/routers/ingest.py` | 已切分 chunks 的底层 re-ingest |
| `backend/api/routers/rag.py` | 语义检索与生成式问答 |
| `backend/api/routers/auth.py` | 未接入的旧 User/Bearer 草稿；`main.py` 未注册 |
| `backend/api/routers/users.py` | 未接入的旧 User 资料/API Key 草稿；`main.py` 未注册 |

### Core

| 文件 | 说明 |
|---|---|
| `backend/core/__init__.py` | Core 包标记 |
| `backend/core/config.py` | Pydantic Settings 单一配置源与参数交叉校验 |
| `backend/core/db.py` | SQLAlchemy Engine、Session 工厂和 MySQL URL |
| `backend/core/di.py` | Repository、Client、Service 和存储组件的 DI 工厂 |
| `backend/core/exceptions.py` | Document、Plugin、Security、Milvus 等领域异常 |
| `backend/core/security.py` | AES-256-GCM、SHA-256、Plugin ID/Secret 工具 |
| `backend/core/url_normalization.py` | HTTP(S) URL 校验、跟踪参数清理与网页身份规范化 |

### Models 与契约

| 文件 | 说明 |
|---|---|
| `backend/models/__init__.py` | 对外导出 ORM 与常量 |
| `backend/models/base.py` | SQLAlchemy Declarative Base |
| `backend/models/document.py` | `documents` ORM、Document 状态与来源类型 |
| `backend/models/plugin.py` | `plugin_workspaces` ORM 与 Workspace 状态 |
| `backend/models/ingest_job.py` | `ingest_jobs` ORM、任务类型与状态机 |
| `backend/models/ingest_job_api_schema.py` | 任务进度/结果 API Schema |
| `backend/models/milvus_dto.py` | `ChunkVector` / `ChunkSearchResult` 严格 DTO |
| `backend/models/api_schema.py` | Ingest、RAG、Plugin API Schema |
| `backend/models/document_api_schema.py` | Document、Upload、Clip、列表与详情 Schema |
| `backend/models/user.py` | 未接入的旧 User/Bearer 草稿 ORM |

### Services

| 文件 | 说明 |
|---|---|
| `backend/services/__init__.py` | Service 包标记 |
| `backend/services/plugin_service.py` | Workspace 注册、认证、改名、API Key 处理 |
| `backend/services/document_upload.py` | 可分阶段的文件校验/落盘与解析/切块/入库编排 |
| `backend/services/document_ingest.py` | Document 状态机和 ingest 成功/失败收敛 |
| `backend/services/document_delete.py` | Milvus → 文件 → MySQL 的幂等文档删除 |
| `backend/services/workspace_delete.py` | 分批清理文档后删除 Workspace |
| `backend/services/web_clip.py` | 网页文档创建/复用、切块、入库与元数据更新 |
| `backend/services/ingest.py` | old IDs → upsert new → delete stale |
| `backend/services/ingest_job.py` | 任务创建、Redis 入队、归属查询与重试编排 |
| `backend/services/rag.py` | Query 向量化、范围过滤、搜索和 metadata 组装 |
| `backend/services/rag_answer.py` | Retrieval、Context、Prompt、LLM、Sources 编排 |
| `backend/services/user_service.py` | 未接入的旧 User/Bearer 草稿 Service |

### Repository

| 文件 | 说明 |
|---|---|
| `backend/repositories/__init__.py` | Repository 包标记 |
| `backend/repositories/mysql/__init__.py` | 导出 Document Repository 接口与实现 |
| `backend/repositories/mysql/protocol.py` | Document Repository Protocol |
| `backend/repositories/mysql/impl.py` | SQLAlchemy Document Repository 与 Workspace 过滤 |
| `backend/repositories/mysql/plugin_protocol.py` | Plugin Repository Protocol |
| `backend/repositories/mysql/plugin_impl.py` | SQLAlchemy Plugin Workspace Repository |
| `backend/repositories/mysql/ingest_job_protocol.py` | 异步入库任务 Repository Protocol |
| `backend/repositories/mysql/ingest_job_impl.py` | 任务创建、原子 claim、终态与崩溃恢复持久化 |
| `backend/repositories/mysql/user_protocol.py` | 未接入的旧 User Repository Protocol |
| `backend/repositories/mysql/user_impl.py` | 未接入的旧 User Repository 实现 |
| `backend/repositories/milvus/__init__.py` | 导出 Milvus Protocol、实现与初始化器 |
| `backend/repositories/milvus/protocol.py` | Milvus Repository Protocol |
| `backend/repositories/milvus/impl.py` | pymilvus 查询、upsert、删除与搜索实现 |
| `backend/repositories/milvus/initializer.py` | Collection、HNSW/倒排索引的幂等初始化 |

### Client、解析、切块与存储

| 文件 | 说明 |
|---|---|
| `backend/clients/__init__.py` | Client 包标记 |
| `backend/clients/embedding.py` | 百炼 Embedding Client、batch 与 Key 隔离 |
| `backend/clients/llm.py` | LLM Protocol 与百炼 Chat Completion 实现 |
| `backend/parsers/__init__.py` | Parser 包标记 |
| `backend/parsers/protocol.py` | Document Parser Protocol |
| `backend/parsers/text.py` | UTF-8/BOM 文本与 Markdown Parser |
| `backend/chunkers/__init__.py` | Chunker 包标记 |
| `backend/chunkers/protocol.py` | Chunker Protocol |
| `backend/chunkers/recursive.py` | 字符级递归切块，默认 700/100 |
| `backend/storage/__init__.py` | Storage 包标记 |
| `backend/storage/protocol.py` | File Storage Protocol |
| `backend/storage/local.py` | 本地保存/删除与路径穿越防护 |
| `backend/tasks/redis_queue.py` | Redis 待处理/processing 队列、payload TTL 与 ack/recovery |
| `backend/workers/ingest_worker.py` | 异步网页与文件入库 Worker CLI |
| `backend/requirements.txt` | 后端依赖和 Milvus Client 版本约束 |

### 后端测试

| 文件 | 覆盖重点 |
|---|---|
| `backend/tests/test_chunker.py` | Chunk 大小、重叠、顺序和配置边界 |
| `backend/tests/test_text_parser.py` | UTF-8/BOM、扩展名和异常 |
| `backend/tests/test_file_storage.py` | 保存/删除、幂等与路径穿越 |
| `backend/tests/test_embedding_client.py` | 批处理、维度、异常和 Key 隔离 |
| `backend/tests/test_llm_client.py` | LLM 参数、响应和异常包装 |
| `backend/tests/test_security.py` | AES-GCM、哈希、随机凭证与篡改检测 |
| `backend/tests/test_url_normalization.py` | URL 规范化、跟踪参数和非法 URL 边界 |
| `backend/tests/test_document_repository.py` | CRUD、筛选、分页和 Workspace 隔离 |
| `backend/tests/test_plugin_repository.py` | Plugin Repository 数据契约 |
| `backend/tests/test_plugin_service.py` | 名称、认证、API Key 与敏感信息保护 |
| `backend/tests/test_plugin_isolation.py` | Workspace 数据隔离 |
| `backend/tests/test_document_upload_service.py` | 上传编排、补偿和状态机 |
| `backend/tests/test_document_ingest_service.py` | Ingest 状态、重试和删除互斥 |
| `backend/tests/test_document_delete_service.py` | 删除顺序、幂等和失败收敛 |
| `backend/tests/test_workspace_delete_service.py` | Workspace 级联删除和重试 |
| `backend/tests/test_web_clip_service.py` | URL 复用、状态与隔离 |
| `backend/tests/test_rag_service.py` | 检索范围、后过滤、metadata 与隔离 |
| `backend/tests/test_rag_answer_service.py` | Context、空结果、Sources 与 LLM |
| `backend/tests/test_ingest_api.py` | `/ingest/page` 契约与错误映射 |
| `backend/tests/test_rag_api.py` | `/rag/search` 契约和隔离 |
| `backend/tests/test_rag_answer_api.py` | `/rag/ask` 契约和异常映射 |
| `backend/tests/test_document_api.py` | Document CRUD、列表/详情与分页 API |
| `backend/tests/test_document_upload_api.py` | 同步/异步 multipart 上传 API |
| `backend/tests/test_web_clip_api.py` | Web Clip Schema 与响应 |
| `backend/tests/test_plugin_api.py` | Plugin Workspace API |
| `backend/tests/test_ingest_job_repository.py` | 任务状态机、原子 claim、恢复与归属 |
| `backend/tests/test_ingest_job_service.py` | 入队失败补偿、payload 安全与重试上限 |
| `backend/tests/test_ingest_job_api.py` | 异步提交、进度查询、重试和错误映射 |
| `backend/tests/test_redis_ingest_queue.py` | Redis 原子入队、processing ack 与恢复 |
| `backend/tests/test_ingest_worker.py` | Worker 成功、失败、重复投递与中断恢复 |
| `backend/tests/test_evaluation_baseline.py` | 指标、数据对齐和隔离泄漏计算 |
| `backend/tests/test_auth_api.py` | 未接入 User/Bearer 草稿测试；当前收集失败 |
| `backend/tests/test_user_repository.py` | 未接入 User Repository 草稿测试；当前收集失败 |
| `backend/tests/test_user_service.py` | 未接入 User Service 草稿测试；当前收集失败 |

## `extension/` — 浏览器扩展

| 文件 | 说明 |
|---|---|
| `extension/manifest.json` | Manifest V3、Side Panel、Service Worker 与权限 |
| `extension/config.js` | 后端地址、存储键、Session 上限等常量 |
| `extension/url-utils.js` | 与后端对齐的 URL 规范化查重辅助 |
| `extension/background.js` | Side Panel 行为和 Tab 消息广播 |
| `extension/content.js` | DOM 正文抽取、噪声清理和 SPA URL 监听 |
| `extension/api-client.js` | Plugin Header、JSON/multipart 请求与错误分类 |
| `extension/session-store.js` | Session、当前 Session 和 Tab Binding 存储层 |
| `extension/sidepanel.html` | 注册、剪藏、聊天、知识库和设置视图 |
| `extension/sidepanel.css` | Side Panel 样式 |
| `extension/sidepanel.js` | Side Panel 状态、事件和业务交互 |
| `extension/popup.html` | Popup 轻量入口结构 |
| `extension/popup.css` | Popup 样式 |
| `extension/popup.js` | 页面预览、快捷剪藏和打开 Side Panel |
| `extension/tests/url-utils.test.js` | 扩展 URL 规范化的 Node.js 回归测试 |

## `alembic/` — 数据库迁移

| 文件 | 说明 |
|---|---|
| `alembic/env.py` | 加载 ORM metadata，按环境构造数据库 URL |
| `alembic/script.py.mako` | 新迁移模板 |
| `alembic/versions/0001_create_documents.py` | 初始 `documents` 表 |
| `alembic/versions/0002_add_document_file_metadata.py` | 增加大小、MIME 和错误信息 |
| `alembic/versions/0003_add_document_source_metadata.py` | 增加标题、URL 和来源类型 |
| `alembic/versions/0004_create_users.py` | 历史 User 表迁移 |
| `alembic/versions/0005_documents_user_id_not_null.py` | 历史 `documents.user_id` 非空化 |
| `alembic/versions/0006_user_identity_rework.py` | 历史用户名/密码身份重构 |
| `alembic/versions/0007_plugin_workspace.py` | 创建 Workspace，增加并回填 `documents.plugin_id` |
| `alembic/versions/0008_documents_user_id_default.py` | 为旧 `user_id` 设置默认 0 |
| `alembic/versions/0009_create_ingest_jobs.py` | 创建持久化异步入库任务表 |

## `evaluation/` — Retrieval 基线

| 文件 | 说明 |
|---|---|
| `evaluation/__init__.py` | Evaluation 包标记 |
| `evaluation/README.md` | 数据验证、对齐与基线执行说明 |
| `evaluation/baseline.py` | 指标计算、样本运行和结果汇总 |
| `evaluation/run_baseline.py` | 调用后端执行 Retrieval 基线的 CLI |
| `evaluation/validate_datasets.py` | JSONL Schema、占位符和一致性校验 |
| `evaluation/align_datasets.py` | 用真实 ID 替换占位符 |
| `evaluation/validate_runtime_alignment.py` | 对照 MySQL/Milvus 验证映射与归属 |
| `evaluation/render_report.py` | 将基线 JSON 渲染为 Markdown |

### 数据集

| 文件 | 说明 |
|---|---|
| `evaluation/datasets/DATASET_MANIFEST.md` | 150 条样本、占位符、源文档和复核状态 |
| `evaluation/datasets/rag_eval.jsonl` | 70 条主 Retrieval/RAG 样本 |
| `evaluation/datasets/negative_eval.jsonl` | 10 条拒答与幻觉负例 |
| `evaluation/datasets/isolation_eval.jsonl` | 70 条 Workspace 隔离样本 |
| `evaluation/datasets/chunk_gold_annotations.json` | Chunk Gold 人工标注与复核数据 |

### 评测源文档

`evaluation/datasets/source_docs/plugin-a/` 是 LangChain/LangGraph Workspace：

| 文件 | 主题 |
|---|---|
| `A01_langgraph_stategraph_definition.md` | StateGraph 定义与 reducer |
| `A02_langgraph_nodes_best_practices.md` | Node 编写实践 |
| `A03_langgraph_conditional_edges.md` | 条件边与路由 |
| `A04_langgraph_end_exit_conditions.md` | END 与退出条件 |
| `A05_langgraph_memory_checkpointer.md` | Memory 与 Checkpointer 基础 |
| `A06_langchain_runnable_lcel.md` | Runnable 与 LCEL |
| `A07_langchain_prompt_templates.md` | Prompt Template |
| `A08_langchain_output_parsers.md` | Output Parser 与降级 |
| `A09_langchain_retrievers_comparison.md` | Retriever 对比 |
| `A10_langchain_embeddings_bailian_v3.md` | 百炼 Embedding v3 |
| `A11_langgraph_tool_calling_mechanism.md` | Tool Calling |
| `A12_agent_executor_vs_langgraph.md` | AgentExecutor 与 LangGraph 对比 |
| `A13_langgraph_multi_agent_supervisor.md` | Multi-Agent Supervisor |
| `A14_langgraph_human_in_the_loop.md` | Human in the Loop |
| `A15_langgraph_streaming_modes.md` | Streaming 模式 |
| `A16_langgraph_persistence_production_best_practices.md` | 生产级持久化 |
| `A17_langchain_callbacks_langsmith_tracing.md` | Callback 与 LangSmith tracing |
| `A18_langchain_error_handling_fallbacks.md` | 错误处理与 fallback |
| `A19_langchain_chunking_strategies.md` | Chunking 策略 |
| `A20_langgraph_production_deployment_k8s.md` | K8s 生产部署 |

`evaluation/datasets/source_docs/plugin-b/` 是 Java/Spring Workspace：

| 文件 | 主题 |
|---|---|
| `B01_springboot_autoconfiguration.md` | Spring Boot 自动配置 |
| `B02_spring_ioc_bean_lifecycle.md` | IoC Bean 生命周期 |
| `B03_spring_dependency_injection_modes.md` | 依赖注入模式 |
| `B04_spring_application_event_stategraph.md` | Application Event 与状态图术语陷阱 |
| `B05_spring_bean_factorybean_comparison.md` | BeanFactory / FactoryBean |
| `B06_spring_mvc_rest_controllers.md` | MVC REST Controller |
| `B07_resttemplate_vs_webclient_migration.md` | RestTemplate / WebClient 迁移 |
| `B08_spring_webflux_reactive_core.md` | WebFlux 响应式核心 |
| `B09_spring_filter_interceptor_comparison.md` | Filter / Interceptor |
| `B10_spring_actuator_health_metrics.md` | Actuator 健康与指标 |
| `B11_spring_data_jpa_nplusone.md` | JPA N+1 |
| `B12_hibernate_entity_states_flush.md` | Hibernate 状态与 flush |
| `B13_spring_transactional_traps_best_practices.md` | Transactional 陷阱 |
| `B14_flyway_database_migration_best_practices.md` | Flyway 迁移实践 |
| `B15_spring_jdbctemplate_namedparameter.md` | JdbcTemplate / NamedParameter |
| `B16_spring_security_jwt_filterchain.md` | Spring Security JWT FilterChain |
| `B17_spring_boot_testing_slices.md` | Spring Boot Test Slices |
| `B18_spring_aop_aspect_around_audit.md` | AOP Around 与审计 |
| `B19_springboot_deploy_docker_jvm_k8s.md` | Docker/JVM/K8s 部署 |
| `B20_spring_ai_vs_langchain_python_rag.md` | Spring AI 与 LangChain RAG 对比陷阱 |

## 根级评测测试

| 文件 | 说明 |
|---|---|
| `tests/conftest.py` | 根级 pytest 路径/fixture 配置 |
| `tests/test_render_report.py` | Markdown 报告结构与输出测试 |

## `docs/` — 设计记录与手册

| 文件 | 说明 |
|---|---|
| `docs/ARCHITECTURE.md` | 较早阶段架构快照；部分内容已过时 |
| `docs/PHASE0_ARCHITECTURE.md` | 初始阶段架构方案 |
| `docs/PHASE2_DATA_MODEL.md` | Phase 2 关系数据模型设计 |
| `docs/PHASE2_MILVUS_SCHEMA.md` | Milvus Schema、索引和约束设计 |
| `docs/PHASE2.3_MILVUS_REPOSITORY_DESIGN.md` | Milvus Repository 分层设计 |
| `docs/REAL_BASELINE_RUNBOOK.md` | 真实环境评测对齐和运行手册 |
| `docs/INTERVIEW_MATERIALS.md` | 项目讲解/面试材料，不是运行规范 |

## 运行时目录

| 路径 | 说明 |
|---|---|
| `uploads/` | 上传文件存储；Git 忽略 |
| `evaluation/private/` | 本地 Plugin Secret 等凭证；Git 忽略 |
| `evaluation/aligned*/` | 运行时 ID 对齐数据；Git 忽略 |
| `evaluation/reports/` | 基线输出和报告；Git 忽略 |
