# Web RAG Clipper 文件索引

本索引按职责列出当前运行链路和主要维护入口，不重复展开每一份评测语料。运行时生成的 `.env`、`.venv/`、`uploads/`、缓存和 evaluation 私有输出均被 Git 忽略，不属于源代码。

## 根目录

| 文件 | 说明 |
|---|---|
| `README.md` | 项目定位、快速启动、模型配置、使用流程、测试与排障 |
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
| `backend/api/routers/plugins.py` | Workspace 注册、详情、改名、多服务商模型配置与删除 |
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
| `backend/models/model_provider.py` | Embedding/LLM 服务商预设、端点校验、加密载荷与向量空间指纹 |
| `backend/models/ingest_job.py` | `ingest_jobs` ORM、任务类型与状态机 |
| `backend/models/ingest_job_api_schema.py` | 任务进度/结果 API Schema |
| `backend/models/milvus_dto.py` | `ChunkVector` / `ChunkSearchResult` 严格 DTO |
| `backend/models/api_schema.py` | Ingest、RAG、Plugin 与模型配置 API Schema |
| `backend/models/document_api_schema.py` | Document、Upload、Clip、列表与详情 Schema |
| `backend/models/user.py` | 未接入的旧 User/Bearer 草稿 ORM |

### Services

| 文件 | 说明 |
|---|---|
| `backend/services/__init__.py` | Service 包标记 |
| `backend/services/plugin_service.py` | Workspace 注册、认证、改名、双模型配置加密与向量空间切换保护 |
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
| `backend/clients/embedding.py` | 多服务商 OpenAI-compatible Embedding Client、batch、维度与 Key 隔离 |
| `backend/clients/llm.py` | LLM Protocol 与多服务商 OpenAI-compatible Chat Completion 实现 |
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
| `backend/tests/test_model_provider.py` | 服务商预设、端点安全、配置加密兼容与向量空间切换保护 |
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
| `extension/extractor.js` | 可测试的正文候选评分、DOM 清噪与结构化文本序列化引擎 |
| `extension/content.js` | 提取消息入口、降级处理和 SPA URL 监听 |
| `extension/api-client.js` | Plugin Header、JSON/multipart 请求与错误分类 |
| `extension/session-store.js` | Session、当前 Session 和 Tab Binding 存储层 |
| `extension/sidepanel.html` | 注册、剪藏、聊天、知识库和设置视图 |
| `extension/sidepanel.css` | Side Panel 样式 |
| `extension/sidepanel.js` | Side Panel 状态、事件和业务交互 |
| `extension/popup.html` | Popup 轻量入口结构 |
| `extension/popup.css` | Popup 样式 |
| `extension/popup.js` | 页面预览、快捷剪藏和打开 Side Panel |
| `extension/tests/extractor.test.js` | 正文候选评分、噪声识别和文本规范化回归测试 |
| `extension/tests/url-utils.test.js` | 扩展 URL 规范化的 Node.js 回归测试 |
| `extension/tests/session-store.test.js` | 文件上传任务持久化回归测试 |

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
| `alembic/versions/0010_expand_model_config_ciphertext.py` | 扩展加密模型配置字段并增加 Embedding 指纹 |

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

| 目录 | 内容 |
|---|---|
| `evaluation/datasets/source_docs/plugin-a/` | 20 篇 LangChain/LangGraph 主题语料（A01–A20） |
| `evaluation/datasets/source_docs/plugin-b/` | 20 篇 Java/Spring 主题语料（B01–B20） |

具体主题、占位符和人工复核状态以 `evaluation/datasets/DATASET_MANIFEST.md` 为准。

## 根级评测测试

| 文件 | 说明 |
|---|---|
| `tests/conftest.py` | 根级 pytest 路径/fixture 配置 |
| `tests/test_render_report.py` | Markdown 报告结构与输出测试 |

## `docs/` — 历史设计记录

该目录用于设计追溯，不是当前运行规范。当前行为以根目录 `README.md`、`ARCHITECTURE.md` 和代码为准。

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
