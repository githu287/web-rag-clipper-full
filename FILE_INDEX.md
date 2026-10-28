# Web RAG Clipper 文件索引

本索引按职责列出当前运行链路和主要维护入口，不重复展开每一份评测语料。运行时生成的 `.env`、`.venv/`、`uploads/`、缓存和 evaluation 私有输出均被 Git 忽略，不属于源代码。

## 根目录

| 文件 | 说明 |
|---|---|
| `README.md` | 项目定位、快速启动、模型配置、使用流程、测试与排障 |
| `LIVE_ACCEPTANCE.md` | 本机 0012 升级备份、真实模型/浏览器/故障验收及向量读取一致性修复 |
| `.github/workflows/ci.yml` | push/PR/手动触发的 Linux/Windows 离线回归，以及 Linux Chromium 真实扩展提取测试 |
| `scripts/check.py` | 本地与 CI 统一离线检查入口，自动发现 Node 测试并传播失败 |
| `scripts/check_capture_browser.py` | 无后端/凭证的真实 Chrome/Chromium 扩展提取、跨域 iframe、延迟渲染与区域交互测试 |
| `CLIPPING_COMPATIBILITY.md` | 四种剪藏方式、依赖来源、真实浏览器验证和网站适配边界 |
| `SITE_AUDIT_2026-10-28.md` | 13 个公开网页的实测结果、代码保真修复、访问受限与图片边界 |
| `scripts/audit_capture_sites.py` | 按需在线样本审计，记录可见块/源码保留、标题、噪声、状态与耗时；运行数据默认不入库 |
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
| `backend/api/routers/__init__.py` | Router 子包标记；文件内按 prefix 列出当前 6 个路由器 |
| `backend/api/routers/plugins.py` | Workspace 注册、详情、改名、多服务商模型配置与删除 |
| `backend/api/routers/documents.py` | 文档列表/详情、创建、上传、ingest 与删除 |
| `backend/api/routers/clips.py` | 网页正文剪藏：同步入库与异步提交（提交前校验 Key 配置与 Workspace 配额） |
| `backend/api/routers/jobs.py` | 异步入库任务查询与失败重试 |
| `backend/api/routers/ingest.py` | 已切分 chunks 的底层 re-ingest |
| `backend/api/routers/rag.py` | 语义检索与生成式问答 |

### Core

| 文件 | 说明 |
|---|---|
| `backend/core/__init__.py` | Core 包标记 |
| `backend/core/config.py` | Pydantic Settings 单一配置源与参数交叉校验 |
| `backend/core/request_budget.py` | 根据模型超时及重试配置计算公开客户端等待预算 |
| `backend/core/db.py` | SQLAlchemy Engine、Session 工厂和 MySQL URL |
| `backend/core/di.py` | Repository、Client、Service 和存储组件的 DI 工厂 |
| `backend/core/exceptions.py` | Document、Plugin、Security、Milvus 等领域异常 |
| `backend/core/security.py` | AES-256-GCM、SHA-256、Plugin ID/Secret 工具 |
| `backend/core/url_normalization.py` | HTTP(S) URL 校验、跟踪参数清理、网页身份规范化（保留哈希路由 `#/...`，剥普通锚点） |

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

### Services

| 文件 | 说明 |
|---|---|
| `backend/services/__init__.py` | Service 包标记 |
| `backend/services/plugin_service.py` | Workspace 注册、认证、改名、双模型配置加密与向量空间切换保护 |
| `backend/services/document_upload.py` | 可分阶段的文件校验/落盘与解析/切块/入库编排（落盘前校验 Workspace 文档数配额） |
| `backend/services/document_ingest.py` | Document 状态机和 ingest 成功/失败收敛 |
| `backend/services/document_delete.py` | Milvus → 文件 → MySQL 的幂等文档删除 |
| `backend/services/workspace_delete.py` | 分批清理文档后删除 Workspace |
| `backend/services/web_clip.py` | 网页文档创建/复用、切块、入库与元数据更新（新建受 Workspace 配额限制，同 URL 复用不受限） |
| `backend/services/ingest.py` | old IDs → upsert new → delete stale |
| `backend/services/ingest_job.py` | 任务创建、Redis 入队、归属查询与重试编排 |
| `backend/services/rag.py` | Query 向量化、检索文档数上界、范围过滤、搜索和 metadata 组装 |
| `backend/services/rag_answer.py` | Retrieval、Context、Prompt、LLM、Sources 编排 |

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
| `backend/repositories/mysql/workspace_lock.py` | 模型配置与文档写入共享的 Workspace 行锁、Embedding 快照校验 |
| `backend/repositories/milvus/__init__.py` | 导出 Milvus Protocol、实现与初始化器 |
| `backend/repositories/milvus/protocol.py` | Milvus Repository Protocol |
| `backend/repositories/milvus/impl.py` | pymilvus 查询、upsert、删除与搜索实现 |
| `backend/repositories/milvus/initializer.py` | Collection、HNSW/倒排索引的幂等初始化 |

### Client、解析、切块与存储

| 文件 | 说明 |
|---|---|
| `backend/clients/__init__.py` | Client 包标记 |
| `backend/clients/embedding.py` | 多服务商 OpenAI-compatible Embedding Client、batch、维度与 Key 隔离（缓存按 LRU 淘汰并关闭连接池） |
| `backend/clients/llm.py` | LLM Protocol 与多服务商 OpenAI-compatible Chat Completion 实现（缓存按 LRU 淘汰并关闭连接池） |
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
| `backend/workers/ingest_worker.py` | 异步网页与文件入库 Worker CLI（SIGTERM 在任务边界优雅退出） |
| `backend/requirements.txt` | 后端依赖和 Milvus Client 版本约束 |

### 后端测试

| 文件 | 覆盖重点 |
|---|---|
| `backend/tests/test_chunker.py` | Chunk 大小、重叠、顺序和配置边界 |
| `backend/tests/test_text_parser.py` | UTF-8/BOM、扩展名和异常 |
| `backend/tests/test_file_storage.py` | 保存/删除、幂等与路径穿越 |
| `backend/tests/test_embedding_client.py` | 批处理、维度、异常、Key 隔离与缓存淘汰 |
| `backend/tests/test_llm_client.py` | LLM 参数、响应、异常包装与缓存淘汰 |
| `backend/tests/test_model_provider.py` | 服务商预设、端点安全、配置加密兼容与向量空间切换保护 |
| `backend/tests/test_request_budget.py` | 等待预算、SDK 重试预留、浏览器计时上限与公开字段隔离 |
| `backend/tests/test_ingest_recovery_fixes.py` | 统一配额、满额重新剪藏、任务查找隔离、上传竞争补偿与切块边界 |
| `backend/tests/test_security.py` | AES-GCM、哈希、随机凭证与篡改检测 |
| `backend/tests/test_url_normalization.py` | URL 规范化、跟踪参数、哈希路由 fragment 策略（与 `url-utils.test.js` 共享向量）和非法 URL 边界 |
| `backend/tests/test_milvus_repository.py` | Milvus 搜索 limit/ef 参数在连接前校验 |
| `backend/tests/test_milvus_delete_visibility.py` | 删除前强一致枚举 Worker 刚写入的向量，防止遗留孤儿 chunk |
| `backend/tests/test_document_repository.py` | CRUD、筛选、分页和 Workspace 隔离 |
| `backend/tests/test_plugin_repository.py` | Plugin Repository 数据契约 |
| `backend/tests/test_plugin_service.py` | 名称、认证、API Key 与敏感信息保护 |
| `backend/tests/test_plugin_isolation.py` | Workspace 数据隔离 |
| `backend/tests/test_document_upload_service.py` | 上传编排、补偿和状态机 |
| `backend/tests/test_document_ingest_service.py` | Ingest 状态、重试和删除互斥 |
| `backend/tests/test_document_delete_service.py` | 删除顺序、幂等和失败收敛 |
| `backend/tests/test_workspace_delete_service.py` | Workspace 级联删除和重试 |
| `backend/tests/test_web_clip_service.py` | URL 复用、状态与隔离 |
| `backend/tests/test_rag_service.py` | 检索范围、文档数上界、后过滤、metadata 与隔离 |
| `backend/tests/test_rag_answer_service.py` | Context、空结果、Sources 与 LLM |
| `backend/tests/test_ingest_api.py` | `/ingest/page` 契约与错误映射 |
| `backend/tests/test_rag_api.py` | `/rag/search` 契约和隔离 |
| `backend/tests/test_rag_answer_api.py` | `/rag/ask` 契约和异常映射 |
| `backend/tests/test_document_api.py` | Document CRUD、列表/详情与分页 API |
| `backend/tests/test_document_upload_api.py` | 同步/异步 multipart 上传 API |
| `backend/tests/test_web_clip_api.py` | Web Clip Schema 与响应 + 同步 `POST /clips` HTTP（201/429/401/422） |
| `backend/tests/test_plugin_api.py` | Plugin Workspace API |
| `backend/tests/test_error_contract.py` | 异常类 → 状态码 → 响应体形状（`type`/`code`）的表驱动契约与双向守门 |
| `backend/tests/test_ingest_job_repository.py` | 任务状态机、原子 claim、恢复与归属 |
| `backend/tests/test_ingest_job_service.py` | Redis 故障下的持久化接收、payload 安全与重试上限 |
| `backend/tests/test_priority_fixes.py` | 文件归属、崩溃补投、缓存丢失、Embedding 切换交错与旧凭证回归 |
| `backend/tests/test_ingest_job_api.py` | 异步提交、进度查询、重试和错误映射 |
| `backend/tests/test_redis_ingest_queue.py` | Redis 原子入队、processing ack 与恢复 |
| `backend/tests/test_ingest_worker.py` | Worker 成功、失败、重复投递、中断恢复与优雅停机 |
| `backend/tests/test_evaluation_baseline.py` | 指标、数据对齐和隔离泄漏计算 |

## `extension/` — 浏览器扩展

| 文件 | 说明 |
|---|---|
| `extension/manifest.json` | Manifest V3、Side Panel、Service Worker 与权限 |
| `extension/config.js` | 后端地址、存储键、Session 上限等常量 |
| `extension/url-utils.js` | 与后端对齐的 URL 规范化查重辅助 |
| `extension/background.js` | Side Panel 行为和 Tab 消息广播 |
| `extension/extractor.js` | 可测试的正文候选评分、DOM 清噪与结构化文本序列化引擎 |
| `extension/page-capture.js` | Side Panel/Popup 共用的多框架提取、合并去重、长度限制、取消及导航校验 |
| `extension/capture-content.js` | 内容稳定等待、可见框架描述、区域高亮选择与清理 |
| `extension/vendor/readability/` | 本地打包的 Mozilla Readability 0.6.0、来源/完整性摘要及许可证 |
| `extension/tests/page-capture.test.js` | 框架筛选、去重、长度、导航、受限提示及取消回归 |
| `extension/tests/capture-driver.html` | 浏览器测试的扩展上下文入口，不存储 Workspace 凭证 |
| `extension/tests/site-samples.json` | 线上公开网页样本、参照正文与标题选择器，以及噪声检查项 |
| `extension/tests/site-fixtures.json` | 11 份原创 DOM 模式回归样本，复现站点问题且不复制原文；由真实浏览器 CI 执行 |
| `extension/content.js` | 提取消息入口、降级处理和 SPA URL 监听 |
| `extension/api-client.js` | Plugin Header、JSON/multipart 请求与错误分类 |
| `extension/clip-detect.js` | 按 URL 探测是否已剪藏，带短 TTL 缓存 |
| `extension/session-store.js` | Session、当前 Session 和 Tab Binding 存储层 |
| `extension/sidepanel.html` | 注册、剪藏、聊天、知识库和设置视图 |
| `extension/sidepanel.css` | Side Panel 样式 |
| `extension/sidepanel.js` | Side Panel 状态、事件和业务交互 |
| `extension/popup.html` | Popup 轻量入口结构 |
| `extension/popup.css` | Popup 样式 |
| `extension/popup.js` | 页面预览、快捷剪藏和打开 Side Panel |
| `extension/tests/extractor.test.js` | 正文候选评分、噪声识别和文本规范化回归测试 |
| `extension/tests/url-utils.test.js` | 扩展 URL 规范化的 Node.js 回归测试 |
| `extension/tests/session-store.test.js` | 上传任务持久化与 Session 并发保存合并回归测试 |
| `extension/tests/api-client.test.js` | JSON/上传错误分类、响应正文超时、模型等待预算与身份切换回归 |
| `extension/tests/clip-detect.test.js` | 剪藏探测缓存 TTL、跨上下文保底与失败不缓存的回归测试 |
| `extension/tests/chat-request.test.js` | 会话切换、页面快照、错误归属与旧请求清理交错回归 |
| `extension/tests/library-retry.test.js` | 服务端任务恢复、来源提示、进行中任务轮询与本地存储失败回归 |

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
| `alembic/versions/0011_durable_ingest_payload.py` | 任务投递 payload 与状态扫描索引，支持可靠补投 |

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

## 运行时目录

| 路径 | 说明 |
|---|---|
| `uploads/` | 上传文件存储；Git 忽略 |
| `evaluation/private/` | 本地 Plugin Secret 等凭证；Git 忽略 |
| `evaluation/aligned*/` | 运行时 ID 对齐数据；Git 忽略 |
| `evaluation/reports/` | 基线输出和报告；Git 忽略 |

## Reliability maintenance

| File | Purpose |
|---|---|
| `conftest.py` | Disable personal dotenv loading in tests |
| `requirements-dev.txt` | Runtime and pytest dependencies |
| `backend/tests/test_ingest_reliability.py` | Persistent state, recovery, ownership and event loop regressions |
| `backend/services/rag_context.py` | Context 字符预算、正文去重与来源编号一致性 |
| `backend/tests/test_rag_context.py` | 截断来源、空证据、元数据预算与日志回归 |

## 两版整合补充索引

| 文件 | 说明 |
|---|---|
| `backend/api/upload.py` | multipart 临时文件限读、大小未知及短读取处理，最多消费文件上限加 1 字节 |
| `backend/clients/client_cache.py` | 带请求租用计数的 LRU；安全淘汰、延迟关闭与退出清理 |
| `backend/tasks/payload.py` | 从持久化任务恢复输入、检查网页正文原始有效期 |
| `backend/tests/test_client_cache.py` | 多线程 SDK 请求、缓存竞争、淘汰与 API/Worker 生命周期清理 |
| `backend/tests/test_delete_reliability.py` | 删除期间事件循环响应、剩余文档保护和并发新增后的重试收敛 |
| `backend/tests/test_upload_limits.py` | 上传限读、字节边界、ASGI 并发响应与调用方取消后的受理完整性 |
| `backend/tests/test_submission_reliability.py` | 入队响应丢失、Worker 状态保护、上传 API 补偿及失败事务回滚 |
| `backend/tests/test_durable_publication.py` | 数据库提交后进程中断、Redis 丢失、分批补发、正文过期与成功清理 |
| `backend/tests/test_durable_payload_migration.py` | 两种 0011 结构升级至 0012、已有期限保留、历史回填及 MySQL SQL 编译 |
| `extension/tests/sidepanel-jobs.test.js` | 网络波动恢复、轮询重试上限、上传恢复与剪藏绑定竞争 |
| `alembic/versions/0012_ingest_payload_deadline.py` | 正文期限与索引，在线兼容本地及远端两种 0011 结构 |
| `VERSION_COMPARISON.md` | 本地/远端版本取舍、整合决策、验证结果与边界 |
| `LOCAL_DEVELOPMENT.md` | 可跨机器复用的环境准备、迁移与启动步骤 |
| `DEVELOPMENT_NOTES.md` | 远端原开发环境的历史接手记录 |
| `PROJECT_REVIEW.md` | 历史审查问题及对应修复记录 |
| `docker-compose.override.yml` | 沿用远端固定摘要的 MinIO 开发镜像覆盖 |
