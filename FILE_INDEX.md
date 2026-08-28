# 项目文件索引

本文件列出 Web RAG Clipper 项目中每一个文件及其作用。

---

## 根目录

| 文件 | 说明 |
|---|---|
| `.env.example` | 环境变量模板，包含全部配置项及注释说明 |
| `.env` | 实际运行环境变量（由 `.env.example` 复制后填写，不提交 Git） |
| `README.md` | 项目说明文档：功能特性、技术栈、架构图、快速开始、API 一览 |
| `FILE_INDEX.md` | 本文件，项目全文件索引 |
| `docker-compose.yml` | Docker Compose 编排：MySQL / Redis / etcd / MinIO / Milvus 五大服务 |
| `alembic.ini` | Alembic 配置文件，`sqlalchemy.url` 留空由 `env.py` 运行时注入 |

---

## Alembic 数据库迁移

| 文件 | 说明 |
|---|---|
| `alembic/env.py` | Alembic 迁移环境入口，从 Settings 构建 MySQL URL，加载 ORM metadata |
| `alembic/script.py.mako` | Alembic 迁移脚本 Mako 模板，生成新迁移时使用的骨架 |
| `alembic/versions/0001_create_documents.py` | 创建 `documents` 表（id, filename, file_path, status, chunk_count, created_at, updated_at） |
| `alembic/versions/0002_add_document_file_metadata.py` | 为 `documents` 添加 `file_size` / `mime_type` 字段 |
| `alembic/versions/0003_add_document_source_metadata.py` | 为 `documents` 添加 `title` / `url` / `source_type` 字段 |
| `alembic/versions/0004_create_users.py` | 创建 `users` 表（后续废弃） |
| `alembic/versions/0005_documents_user_id_not_null.py` | 将 `documents.user_id` 改为 NOT NULL |
| `alembic/versions/0006_user_identity_rework.py` | 用户身份体系重构，移除旧 users 表 |
| `alembic/versions/0007_plugin_workspace.py` | 创建 `plugin_workspaces` 表（Plugin Workspace 多租户） |
| `alembic/versions/0008_documents_user_id_default.py` | 将 `documents.user_id` 改为可空并设默认值 |

---

## 后端 — API 层

| 文件 | 说明 |
|---|---|
| `backend/api/__init__.py` | 空包初始化 |
| `backend/api/deps.py` | FastAPI 依赖注入函数：`get_current_plugin()` 从 `X-Plugin-ID` + `X-Plugin-Secret` 请求头解析当前插件工作空间身份 |
| `backend/api/routers/__init__.py` | 空包初始化 |
| `backend/api/routers/clips.py` | `POST /clips` — Web Clip 网页剪藏路由，接收 URL/标题/正文，`source_type=webpage` 直接入库 |
| `backend/api/routers/documents.py` | 文档管理路由：`POST /documents/upload`（上传入库）、`POST /documents`（创建元数据）、`GET /documents`（分页列表）、`GET /documents/{id}`（详情）、`POST /documents/{id}/ingest`（生命周期 ingest）、`DELETE /documents/{id}`（幂等删除） |
| `backend/api/routers/ingest.py` | `POST /ingest/page` — 底层 chunk 入库路由，re-ingest 链路（query old → upsert new → delete stale） |
| `backend/api/routers/plugins.py` | Plugin Workspace 路由（6 个端点）：`POST /plugins/register`、`GET/PUT/DELETE /plugins/me`、`PUT/DELETE /plugins/me/api-key` |
| `backend/api/routers/rag.py` | RAG 路由：`POST /rag/search`（语义检索）、`POST /rag/ask`（检索 + LLM 问答） |

---

## 后端 — Service 层

| 文件 | 说明 |
|---|---|
| `backend/services/__init__.py` | 空包初始化 |
| `backend/services/plugin_service.py` | PluginService：工作空间注册、身份认证、名称修改、API Key 加密存储/解密/验证、工作空间删除 |
| `backend/services/document_upload.py` | DocumentUploadService：multipart 上传 → 解析 → 切块 → Embedding → 向量入库 → 置 SUCCESS，全链路编排 |
| `backend/services/document_ingest.py` | DocumentIngestService：Document 生命周期 ingest（PENDING → PROCESSING → SUCCESS/FAILED），支持 FAILED 重试 |
| `backend/services/document_delete.py` | DocumentDeleteService：幂等删除（Milvus chunks → 本地文件 → MySQL 行，不存在也返回 204） |
| `backend/services/web_clip.py` | WebClipService：网页剪藏编排，接收 URL/标题/正文 → 切块 → Embedding → 入库，不创建物理文件 |
| `backend/services/ingest.py` | IngestService：底层 chunk 入库逻辑，被 DocumentIngestService 和 WebClipService 复用 |
| `backend/services/rag.py` | RagService：RAG 语义检索，Embedding query → Milvus 候选 → SUCCESS 状态过滤 → Top-K 返回 |
| `backend/services/rag_answer.py` | RagAnswerService：RAG 问答编排，用户问题 → Retrieval（经 RagService）→ 构造 Context → 百炼 qwen-plus LLM → Answer + Sources |

---

## 后端 — Repository 层

| 文件 | 说明 |
|---|---|
| `backend/repositories/__init__.py` | 空包初始化 |
| **MySQL** | |
| `backend/repositories/mysql/__init__.py` | MySQL 子包初始化，重导出 Document/Plugin Repository Protocol + Impl |
| `backend/repositories/mysql/protocol.py` | `DocumentRepository` Protocol 接口定义（CRUD + 状态更新 + 分页查询） |
| `backend/repositories/mysql/impl.py` | `DocumentRepositoryImpl`：DocumentRepository 的 SQLAlchemy 实现 |
| `backend/repositories/mysql/plugin_protocol.py` | `PluginRepository` Protocol 接口定义（plugin_workspaces 表 CRUD） |
| `backend/repositories/mysql/plugin_impl.py` | `PluginRepositoryImpl`：PluginRepository 的 SQLAlchemy 实现 |
| **Milvus** | |
| `backend/repositories/milvus/__init__.py` | Milvus 子包初始化，重导出 MilvusRepository Protocol + Initializer + Impl |
| `backend/repositories/milvus/protocol.py` | `MilvusRepository` Protocol 接口定义（insert / search / delete by page_id） |
| `backend/repositories/milvus/impl.py` | `PyMilvusRepositoryImpl`：MilvusRepository 的 pymilvus 实现 |
| `backend/repositories/milvus/initializer.py` | `MilvusInitializer`：应用启动时幂等创建 `page_chunks` Collection + HNSW Index + Load |
| **Redis** | |
| `backend/repositories/redis/.gitkeep` | 占位文件，Redis 仓储预留目录 |

---

## 后端 — Client 层

| 文件 | 说明 |
|---|---|
| `backend/clients/__init__.py` | 空包初始化 |
| `backend/clients/embedding.py` | `EmbeddingClient`：百炼 `text-embedding-v3` 向量化客户端（OpenAI 兼容协议），批量上限 10 条/请求 |
| `backend/clients/llm.py` | `LLMClient`：百炼 `qwen-plus` LLM 客户端（OpenAI Chat Completions 兼容协议），用于 RAG 问答生成 |

---

## 后端 — Core 基础设施

| 文件 | 说明 |
|---|---|
| `backend/core/__init__.py` | 空包初始化 |
| `backend/core/config.py` | `Settings` 类（Pydantic v2 BaseSettings），全局配置单源：Milvus / MySQL / 百炼 / 切块 / 上传等全部参数 |
| `backend/core/db.py` | SQLAlchemy 2.0 Engine 单例 + `build_mysql_url()` URL 构建函数 |
| `backend/core/di.py` | 依赖注入工厂：`get_*_service()` / `get_*_repository()` 等函数，集中装配 Service/Repository 依赖图 |
| `backend/core/exceptions.py` | 完整异常体系：Document 族 / Plugin 族 / Security 族 / Milvus 族 / Embedding 族 / LLM 族，全部继承自对应基类 |
| `backend/core/security.py` | AES-256-GCM 加密/解密模块：`encrypt_api_key()` / `decrypt_api_key()`，使用 `APP_MASTER_KEY` 主密钥 |

---

## 后端 — Models

| 文件 | 说明 |
|---|---|
| `backend/models/__init__.py` | 包初始化，显式注册 Document / PluginWorkspace ORM 到 Base.metadata |
| `backend/models/base.py` | SQLAlchemy 2.0 `DeclarativeBase`，所有 ORM 模型共享的基类 |
| `backend/models/document.py` | `Document` ORM 模型 + `DocumentStatus` 枚举（PENDING/PROCESSING/SUCCESS/FAILED/DELETING） |
| `backend/models/plugin.py` | `PluginWorkspace` ORM 模型 + `PluginStatus` 枚举（ACTIVE/DISABLED），含 `plugin_secret_hash` / `api_key_ciphertext` / `api_key_nonce` |
| `backend/models/api_schema.py` | 全局 Pydantic API Schema：RAG / Plugin / Ingest 等请求/响应 DTO，全部 `extra="forbid"` |
| `backend/models/document_api_schema.py` | Document 专属 API Schema：`DocumentCreateRequest` / `DocumentResponse` / `DocumentIngestRequest` / `DocumentIngestResponse` / `DocumentListResponse` / `DocumentDetailResponse` |
| `backend/models/milvus_dto.py` | Milvus 数据契约 DTO：`ChunkVector`（写入）/ `ChunkSearchResult`（检索结果），字段集与 Milvus Schema 严格对齐 |

---

## 后端 — 解析 / 切分 / 存储

| 文件 | 说明 |
|---|---|
| `backend/parsers/__init__.py` | 空包初始化 |
| `backend/parsers/protocol.py` | `DocumentParser` Protocol 接口：`parse(file_path) -> str` |
| `backend/parsers/text.py` | `TextParser`：纯文本解析器，支持 `.txt` / `.md` / `.markdown`，读取并 UTF-8 解码 |
| `backend/chunkers/__init__.py` | 空包初始化 |
| `backend/chunkers/protocol.py` | `Chunker` Protocol 接口：`split(text) -> list[str]` |
| `backend/chunkers/recursive.py` | `RecursiveChunker`：递归字符切块器，默认 700 字符/块、100 字符重叠 |
| `backend/storage/__init__.py` | 空包初始化 |
| `backend/storage/protocol.py` | `FileStorage` Protocol 接口：`save()` / `resolve()` / `delete()` |
| `backend/storage/local.py` | `LocalFileStorage`：本地文件系统存储实现，文件存入 `uploads/` 目录，防路径穿越 |

---

## 后端 — 应用入口

| 文件 | 说明 |
|---|---|
| `backend/main.py` | FastAPI 应用入口：`create_app()` 工厂 + `lifespan` 生命周期（启动时 Milvus 初始化） + 全局异常处理器注册（15+ 异常类型 → HTTP 状态码映射） |
| `backend/requirements.txt` | Python 依赖清单：fastapi / uvicorn / sqlalchemy / pymilvus / pydantic / openai / cryptography 等 |

---

## 后端 — 测试

| 文件 | 说明 |
|---|---|
| **API 层测试** | |
| `backend/tests/test_document_api.py` | Document 生命周期 API 测试（POST /documents + POST /documents/{id}/ingest） |
| `backend/tests/test_document_upload_api.py` | 文档上传 API 测试（POST /documents/upload，含空文件/超限/不支持格式） |
| `backend/tests/test_ingest_api.py` | 底层 ingest API 测试（POST /ingest/page） |
| `backend/tests/test_rag_api.py` | RAG 检索 API 测试（POST /rag/search） |
| `backend/tests/test_rag_answer_api.py` | RAG 问答 API 测试（POST /rag/ask） |
| `backend/tests/test_web_clip_api.py` | Web Clip API 测试（POST /clips） |
| `backend/tests/test_plugin_api.py` | Plugin Workspace API 测试（注册/认证/改名/API Key/删除全流程） |
| **Service 层测试** | |
| `backend/tests/test_document_upload_service.py` | DocumentUploadService 单元测试（全链路编排 Mock 验证） |
| `backend/tests/test_document_ingest_service.py` | DocumentIngestService 单元测试（状态流转 + 重试逻辑） |
| `backend/tests/test_document_delete_service.py` | DocumentDeleteService 单元测试（幂等删除顺序验证） |
| `backend/tests/test_rag_service.py` | RagService 单元测试（检索 + 过滤 + Top-K） |
| `backend/tests/test_rag_answer_service.py` | RagAnswerService 单元测试（Context 构造 + 截断 + LLM 调用） |
| `backend/tests/test_web_clip_service.py` | WebClipService 单元测试（网页剪藏编排） |
| `backend/tests/test_plugin_service.py` | PluginService 单元测试（注册/认证/名称归一化/API Key 加密/删除） |
| **数据层测试** | |
| `backend/tests/test_document_repository.py` | DocumentRepository 单元测试（CRUD + 分页 + 状态更新） |
| `backend/tests/test_plugin_repository.py` | PluginRepository 单元测试（plugin_workspaces CRUD） |
| `backend/tests/test_plugin_isolation.py` | Plugin 数据隔离测试（跨 Workspace 文档不可见/不可操作） |
| **组件测试** | |
| `backend/tests/test_text_parser.py` | TextParser 单元测试（各格式解析 + 空文件 + 编码） |
| `backend/tests/test_chunker.py` | RecursiveChunker 单元测试（切块/重叠/空文本/配置校验） |
| `backend/tests/test_file_storage.py` | LocalFileStorage 单元测试（保存/解析/删除/路径穿越防御） |
| `backend/tests/test_embedding_client.py` | EmbeddingClient 单元测试（批量/错误处理/维度校验） |
| `backend/tests/test_llm_client.py` | LLMClient 单元测试（请求/响应/空响应/错误处理） |
| **安全测试** | |
| `backend/tests/test_security.py` | AES-256-GCM 加密/解密测试（密钥校验/解密失败/轮转） |

---

## Chrome 扩展

| 文件 | 说明 |
|---|---|
| `extension/manifest.json` | Manifest V3 声明：名称/版本/权限（`activeTab` / `scripting` / `storage` / `sidePanel` / `tabs`）/host_permissions / side_panel 路径 |
| `extension/background.js` | Service Worker：`chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true })`、Tab 切换时更新 Side Panel 上下文 |
| `extension/sidepanel.html` | Side Panel HTML 结构：欢迎视图 / 被阻止视图 / 应用视图（Chat + Library + Settings） |
| `extension/sidepanel.js` | Side Panel 主逻辑：视图切换、Plugin 注册、聊天问答（流式渲染）、知识库管理（分页/搜索/筛选/删除）、Settings（改名/改 API Key/删除 Workspace） |
| `extension/sidepanel.css` | Side Panel 样式表 |
| `extension/content.js` | Content Script：注入网页提取正文（`article → main → body`，清理 script/style/nav/footer/iframe/广告区，空白归一化） |
| `extension/popup.html` | Popup HTML 结构：轻量入口，引导打开 Side Panel |
| `extension/popup.js` | Popup 逻辑：显示当前页面信息 + 「打开 Side Panel」按钮 |
| `extension/popup.css` | Popup 样式表 |
| `extension/api-client.js` | HTTP 请求封装：统一注入 `X-Plugin-ID` / `X-Plugin-Secret` 请求头，错误处理，响应解析 |
| `extension/session-store.js` | Tab/Session 隔离存储：聊天历史按 Tab ID 独立，Plugin 凭证持久化到 `chrome.storage.local` |
| `extension/config.js` | 后端地址集中配置（`API_BASE_URL`），其他文件不硬编码后端地址 |
| `extension/.gitkeep` | 占位文件 |

---

## 文档

| 文件 | 说明 |
|---|---|
| `docs/ARCHITECTURE.md` | 项目架构文档：分层设计、依赖注入、数据流 |
| `docs/INTERVIEW_MATERIALS.md` | 面试材料：项目亮点、技术决策、问题排查经验总结 |
| `docs/PHASE0_ARCHITECTURE.md` | Phase 0 架构设计文档：初始系统设计与技术选型 |
| `docs/PHASE2_DATA_MODEL.md` | Phase 2 数据模型设计：MySQL 表结构、ORM 映射 |
| `docs/PHASE2_MILVUS_SCHEMA.md` | Phase 2 Milvus Schema 设计：`page_chunks` Collection 字段/索引/参数 |
| `docs/PHASE2.3_MILVUS_REPOSITORY_DESIGN.md` | Phase 2.3 Milvus Repository 设计：Protocol/Impl 分层、Initializer 幂等初始化 |

---

## 其他

| 文件 | 说明 |
|---|---|
| `backend/api/.gitkeep` | 占位文件，确保空目录被 Git 追踪 |
| `backend/clients/.gitkeep` | 同上 |
| `backend/core/.gitkeep` | 同上 |
| `backend/models/.gitkeep` | 同上 |
| `backend/repositories/milvus/.gitkeep` | 同上 |
| `backend/repositories/mysql/.gitkeep` | 同上 |
| `backend/services/.gitkeep` | 同上 |
