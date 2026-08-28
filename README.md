# Web RAG Clipper

面向网页内容剪藏与知识库构建的 RAG 系统。通过 Chrome 扩展（Side Panel）采集网页内容，送入后端知识库完成切分、向量化和 RAG 检索/问答。

**核心能力**：

- **Chrome 扩展 Side Panel**：Tab/Session 隔离、聊天式问答、知识库管理、插件注册，一站式体验
- **Plugin Workspace 多租户**：每个插件工作空间独立身份（`X-Plugin-ID` + `X-Plugin-Secret`）、独立百炼 API Key（AES-256-GCM 加密存储）、数据隔离
- **文档全链路自动化**：上传/剪藏 → 解析 → 切块 → 向量化 → 向量入库，同步完成无需人工介入
- **RAG 检索 + 问答**：Milvus 语义检索 + 百炼 qwen-plus 生成式回答，返回答案与来源引用
- **Docker Compose 一键启动**：MySQL / Redis / etcd / MinIO / Milvus 五大基础设施服务

## 技术栈

| 类别 | 技术 | 说明 |
|---|---|---|
| 语言 | Python 3.11.9 | |
| Web 框架 | FastAPI + Uvicorn | 应用版本 `0.1.0` |
| 数据校验 | Pydantic v2 | 全部 Schema 使用 `extra="forbid"` 防契约漂移 |
| MySQL | MySQL 8.0 + SQLAlchemy 2.0 + PyMySQL | `documents` + `plugin_workspaces` 表 |
| 迁移 | Alembic | head = `0008_documents_user_id_default`（共 8 个迁移） |
| 向量库 | Milvus v2.4.4 (standalone) + pymilvus | `page_chunks` Collection |
| Embedding | 阿里云百炼 `text-embedding-v3` | 1024 维，批量上限 10 条/请求 |
| LLM | 百炼 `qwen-plus`（OpenAI 兼容） | RAG 问答生成 |
| 安全 | AES-256-GCM | 用户 API Key 加密存储（`APP_MASTER_KEY` 主密钥） |
| Redis | redis 7-alpine | compose 已部署，当前预留 |
| 测试 | pytest | 23 个测试文件、476 个测试用例 |
| 扩展 | Chrome MV3 | Side Panel + Background + Content Script |

## 系统架构

```
[Chrome Extension — Side Panel]（MV3）
    │  正文采集 / 剪藏 / 聊天问答 / 知识库管理
    │  身份：X-Plugin-ID + X-Plugin-Secret
    ▼
[FastAPI Backend]
    │  JSON / multipart 请求
    ▼
┌──────────────────────────────────────────────────────┐
│  API 层（FastAPI Router，依赖注入 + Plugin 身份校验）  │
│                                                      │
│  Plugins:  POST /plugins/register                    │
│            GET/PUT/DELETE /plugins/me                 │
│            PUT/DELETE /plugins/me/api-key             │
│  Docs:     POST /documents/upload                     │
│            POST /documents · GET /documents            │
│            GET /documents/{id} · DELETE /documents/{id}│
│            POST /documents/{id}/ingest                │
│  Clips:    POST /clips                                │
│  Ingest:   POST /ingest/page                          │
│  RAG:      POST /rag/search · POST /rag/ask           │
└───────────────────────┬──────────────────────────────┘
                        ▼
┌──────────────────────────────────────────────────────┐
│  Service 层                                          │
│  PluginService          → 工作空间注册/认证/API Key   │
│  DocumentUploadService  → 上传 + 全链路编排           │
│  DocumentIngestService  → 生命周期 + 重试             │
│  DocumentDeleteService  → 幂等删除                   │
│  WebClipService         → 网页剪藏                   │
│  RagService             → RAG 语义检索               │
│  RagAnswerService       → 检索 + LLM 问答            │
└──┬──────┬──────┬──────┬──────┬──────┬───────────────┘
   ▼      ▼      ▼      ▼      ▼      ▼
 安全   存储   解析   切分   向量化  双存储
 AES-256-GCM   LocalFile  TextParser  RecursiveChunker
 (API Key      Storage     (.txt/.md)  (700字/100重叠)
  加密/解密)   (uploads/)
               百炼 Embedding ──► text-embedding-v3 (1024d)
               百炼 LLM ────────► qwen-plus
               MySQL ───────────► documents + plugin_workspaces
               Milvus ──────────► page_chunks（HNSW + COSINE）
```

**核心链路**：

```
注册 Workspace → 配置百炼 API Key → 上传文档/剪藏网页
  → TextParser 解析 → RecursiveChunker 切块
  → 百炼 Embedding → Milvus 写入 chunks → Document 置 SUCCESS
  → RAG Search（语义 Top-K）/ RAG Ask（检索 + qwen-plus 生成回答）
```

## 快速开始

### 前置要求

- Docker Desktop（Windows 需保持运行）
- Python 3.11+（建议 3.11.9）
- 阿里云百炼 API Key（`text-embedding-v3` + `qwen-plus` 访问凭证）

### 1. 启动基础设施

```powershell
docker compose up -d
```

启动的服务：

| 服务 | 宿主端口 | 说明 |
|---|---|---|
| MySQL 8.0 | `33066` | 容器内 3306，数据库 `rag_clipper` |
| Redis 7 | `6379` | 当前预留 |
| etcd v3.5.5 | `2379` | Milvus 元数据 |
| MinIO | `9000` / `9001` | Milvus 对象存储 |
| Milvus v2.4.4 | `19530` | 向量数据库 |

> MySQL 通过宿主 `33066` 暴露（容器内 `3306`）。`.env` 的 `MYSQL_PORT` 需同步为 `33066`。

### 2. 配置环境变量

```powershell
copy .env.example .env
```

编辑 `.env`，至少修改以下项：

| 变量 | 说明 |
|---|---|
| `BAILIAN_API_KEY` | 百炼 API Key（服务端默认 Key，插件工作空间也可配置自己的 Key） |
| `MYSQL_PORT` | 使用本 compose 时填 `33066` |
| `APP_MASTER_KEY` | **必填**。AES-256-GCM 主密钥，用于加密用户 API Key。生成方法见下 |

```powershell
# 生成 APP_MASTER_KEY（32 个 hex 字符 = 32 字节 utf-8）
python -c "import secrets; print(secrets.token_hex(16))"
```

### 3. 安装依赖

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r backend\requirements.txt
```

### 4. 初始化数据库

```powershell
alembic upgrade head
```

执行 8 个迁移（0001 → 0008），创建 `documents`、`plugin_workspaces` 等表。

### 5. 启动 API

```powershell
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

启动时 lifespan 自动初始化 Milvus Collection（幂等：已存在则跳过）。

### 6. 验证

- 打开 http://localhost:8000/docs （Swagger UI，可调试全部接口）
- 注册插件工作空间 → 配置 API Key → 上传文件测试全链路

```powershell
# 注册 Workspace
curl.exe -X POST http://localhost:8000/plugins/register -H "Content-Type: application/json" -d "{\"plugin_name\": \"my-workspace\"}"
# 返回 plugin_id + plugin_secret（保存后用于后续请求头）

# 上传文档（替换 PLUGIN_ID / PLUGIN_SECRET）
curl.exe -F "file=@C:\path\to\sample.txt" -H "X-Plugin-ID: <plugin_id>" -H "X-Plugin-Secret: <plugin_secret>" http://localhost:8000/documents/upload
```

### 7. 加载 Chrome 扩展

1. Chrome 打开 `chrome://extensions/`，开启 **Developer mode**
2. 点击 **Load unpacked**，选择本项目 `extension/` 目录
3. 点击工具栏插件图标，打开 Side Panel
4. 在 Settings 页面注册 Workspace 并配置百炼 API Key
5. 打开任意网页 → 剪藏当前页面 → 在 Chat 中提问

## Chrome 扩展

Manifest V3 Side Panel 扩展，文件位于 `extension/`：

| 文件 | 说明 |
|---|---|
| `manifest.json` | MV3 声明，权限：`activeTab` / `scripting` / `storage` / `sidePanel` / `tabs` |
| `background.js` | Service Worker：Side Panel 行为绑定、Tab 切换监听 |
| `sidepanel.html` / `sidepanel.js` / `sidepanel.css` | Side Panel 主界面：欢迎/注册/应用三视图 |
| `content.js` | 正文提取（`article → main → body`，噪声节点清理） |
| `popup.html` / `popup.js` | Popup 备用入口 |
| `api-client.js` | HTTP 请求封装（Plugin Header 注入、错误处理） |
| `session-store.js` | Tab/Session 隔离存储（聊天历史按 Tab 独立） |
| `config.js` | 后端地址集中配置 |

### Side Panel 功能

- **欢迎页**：首次使用时引导注册 Workspace
- **Chat 页**：聊天式 RAG 问答，支持指定文档范围提问，消息按 Tab 隔离
- **Library 页**：知识库管理，分页浏览/搜索/筛选/删除文档
- **Settings 页**：Workspace 信息、百炼 API Key 配置、删除 Workspace

### 安全设计

- 扩展仅在用户点击时对当前 Tab 注入脚本（`activeTab` 权限），不后台常驻
- AI 生成内容一律 `textContent` 渲染，禁止 `innerHTML` 拼接（防 XSS）
- `plugin_secret` 仅在注册时显示一次，后续请求通过 Header 传递

## API 一览

共 13 个端点。除 `POST /plugins/register` 外，所有端点需要 `X-Plugin-ID` + `X-Plugin-Secret` 请求头。

### Plugin Workspace

| 方法 | 路径 | 说明 | 状态码 |
|---|---|---|---|
| POST | `/plugins/register` | 注册新 Workspace（返回 `plugin_id` + `plugin_secret`） | 201 |
| GET | `/plugins/me` | 获取当前 Workspace 信息 | 200 |
| PUT | `/plugins/me` | 修改显示名 | 200 |
| PUT | `/plugins/me/api-key` | 配置/更换百炼 API Key（会调百炼验证有效性） | 200 |
| DELETE | `/plugins/me/api-key` | 清除 API Key | 204 |
| DELETE | `/plugins/me` | 删除 Workspace（需 `confirm=true` + `plugin_name` 双重确认） | 204 |

### 文档管理

| 方法 | 路径 | 说明 | 状态码 |
|---|---|---|---|
| POST | `/documents/upload` | multipart 上传并完整入库（解析→切块→向量化→入库） | 201 |
| POST | `/documents` | 创建 Document 元数据（`status=PENDING`） | 201 |
| GET | `/documents` | 分页列出当前 Workspace 文档（支持 keyword/status/source_type 筛选） | 200 |
| GET | `/documents/{id}` | 获取单个文档详情 | 200 |
| POST | `/documents/{id}/ingest` | Document 生命周期 ingest（FAILED 可重试） | 200 |
| DELETE | `/documents/{id}` | 删除文档（幂等：不存在也返回 204） | 204 |

### 剪藏 & 入库

| 方法 | 路径 | 说明 | 状态码 |
|---|---|---|---|
| POST | `/clips` | Web Clip 网页剪藏（`source_type=webpage`，正文直接入库） | 201 |
| POST | `/ingest/page` | 底层 chunk 入库（re-ingest：query old → upsert new → delete stale） | 200 |

### RAG

| 方法 | 路径 | 说明 | 状态码 |
|---|---|---|---|
| POST | `/rag/search` | 语义检索（候选 `max(limit,10)` → SUCCESS 过滤 → Top-K） | 200 |
| POST | `/rag/ask` | RAG 问答（检索 → 构造 Context → qwen-plus 生成 → Answer + Sources） | 200 |

### 典型请求/响应

**RAG 问答** `POST /rag/ask`

```json
// 请求
{
  "query": "什么是向量检索",
  "top_k": 5,
  "document_id": null
}

// 200 OK
{
  "answer": "向量检索是一种基于语义相似度的信息检索方法...",
  "sources": [
    {
      "document_id": 1,
      "title": "sample.txt",
      "url": null,
      "chunk_id": "1_3",
      "score": 0.86
    }
  ]
}
```

**RAG 检索** `POST /rag/search`

```json
// 请求
{ "query": "什么是向量检索", "limit": 5 }

// 200 OK
{
  "results": [
    {
      "id": "1_3",
      "page_id": 1,
      "chunk_index": 3,
      "chunk_text": "...",
      "distance": 0.86,
      "document_id": 1,
      "filename": "sample.txt",
      "status": "SUCCESS",
      "created_at": "2026-08-20T10:00:00"
    }
  ]
}
```

> `distance` 为 COSINE similarity（越大越相似，1.0 为完全相似）。

### 错误映射

| HTTP | 场景 |
|---|---|
| 400 | 空文件、路径穿越、删除确认失败、API Key 校验失败 |
| 401 | Plugin 凭证缺失/无效（`X-Plugin-ID` / `X-Plugin-Secret`） |
| 403 | Workspace 被禁用 |
| 404 | Document 不存在（跨 Workspace 也返回 404，不泄露归属） |
| 409 | 名称已占用、API Key 未配置、Document 状态不允许操作 |
| 413 | 文件超过大小上限（默认 2 MB） |
| 415 | 不支持的扩展名（当前仅 `.txt` / `.md` / `.markdown`） |
| 422 | Plugin 名称格式非法 |
| 502 | 百炼 Embedding / LLM 服务异常（可重试） |
| 503 | Milvus / MySQL / Plugin 数据服务异常（可重试） |

## 环境变量

全部配置项见 `.env.example`（复制为 `.env` 后填写，禁止硬编码 API Key）。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `APP_ENV` | `development` | 运行环境 |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` | 服务监听地址 |
| `APP_MASTER_KEY` | 空 | **必填**。AES-256-GCM 主密钥（32 字节），加密用户 API Key |
| `MYSQL_HOST` / `MYSQL_PORT` | `localhost` / `3306` | MySQL 连接（compose 填 `33066`） |
| `MYSQL_USER` / `MYSQL_PASSWORD` | `rag_user` / `rag_password` | MySQL 凭证 |
| `MYSQL_DATABASE` | `rag_clipper` | 数据库名 |
| `MILVUS_HOST` / `MILVUS_PORT` | `localhost` / `19530` | Milvus 连接 |
| `MILVUS_COLLECTION` | `page_chunks` | Milvus Collection 名 |
| `BAILIAN_API_KEY` | 空 | 百炼 API Key（服务端默认 Key） |
| `BAILIAN_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 百炼 OpenAI 兼容端点 |
| `BAILIAN_EMBEDDING_MODEL` | `text-embedding-v3` | Embedding 模型 |
| `BAILIAN_EMBEDDING_DIMENSION` | `1024` | 向量维度（须与 Milvus `embedding.dim` 一致） |
| `BAILIAN_LLM_MODEL` | `qwen-plus` | LLM 模型 |
| `UPLOAD_DIR` | `uploads` | 原始文件存储根目录 |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `700` / `100` | 递归切块参数 |
| `EMBEDDING_BATCH_SIZE` | `10` | Embedding 批量上限（百炼硬限制 10） |
| `MAX_PAGE_CONTENT_BYTES` | `2097152` | 上传文件大小上限（2 MB） |
| `REDIS_*` | — | Redis 连接（当前预留） |
| `CORS_ORIGINS` | `chrome-extension://*` | CORS 配置 |

## 数据模型

### MySQL `plugin_workspaces` 表

| 字段 | 类型 | 说明 |
|---|---|---|
| `plugin_id` | VARCHAR(36) PK | UUID v4 |
| `plugin_name` | VARCHAR(64) | 显示名（原始值） |
| `plugin_name_norm` | VARCHAR(64) UNIQUE | 归一化名（strip + collapse + lower） |
| `plugin_secret_hash` | VARCHAR(255) | bcrypt hash |
| `api_key_ciphertext` | BLOB NULL | AES-256-GCM 加密后的百炼 API Key |
| `api_key_nonce` | BLOB NULL | GCM nonce |
| `status` | VARCHAR(32) | `ACTIVE` / `DISABLED` |
| `created_at` / `updated_at` | DATETIME | 时间戳 |

### MySQL `documents` 表

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INT PK AUTO_INCREMENT | 主键，1:1 对应 Milvus `page_id` |
| `plugin_id` | VARCHAR(36) | 所属 Workspace（FK → plugin_workspaces） |
| `title` | VARCHAR(512) | 文档标题 |
| `filename` | VARCHAR(255) | 文件名 |
| `url` | VARCHAR(2048) NULL | 来源 URL（webpage 类型） |
| `source_type` | VARCHAR(32) | `upload` / `webpage` |
| `status` | VARCHAR(32) | `PENDING / PROCESSING / SUCCESS / FAILED / DELETING` |
| `chunk_count` | INT | 已入库 chunk 数 |
| `file_size` | INT | 文件字节数 |
| `mime_type` | VARCHAR(128) | MIME 类型 |
| `file_path` | VARCHAR(512) | 文件存储路径 |
| `error_message` | TEXT NULL | 失败摘要 |
| `created_at` / `updated_at` | DATETIME | 时间戳 |

### Milvus `page_chunks` Collection

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | VARCHAR(64) PK | `{page_id}_{chunk_index}` |
| `page_id` | INT64 | = `documents.id`（1:1） |
| `chunk_index` | INT64 | chunk 序号（0 起） |
| `chunk_text` | VARCHAR(4096) | 切块文本 |
| `embedding` | FLOAT_VECTOR(1024) | 百炼向量 |

- 向量索引：`HNSW`（metric=COSINE，M=16，efConstruction=200）
- 检索参数：`ef=128`，返回 `id/page_id/chunk_index/chunk_text`，不返回 `embedding`
- 初始化幂等：应用启动时 `MilvusInitializer.initialize()`，Collection 已存在则跳过

## 测试

测试框架：`pytest`，测试文件位于 `backend/tests/`。

```powershell
.venv\Scripts\python.exe -m pytest backend/tests -v
```

覆盖范围（23 个测试文件，476 个测试用例）：

**API 层**（7 个）：
`test_document_api` / `test_document_upload_api` / `test_ingest_api` / `test_rag_api` / `test_rag_answer_api` / `test_web_clip_api` / `test_plugin_api`

**Service 层**（7 个）：
`test_document_upload_service` / `test_document_ingest_service` / `test_document_delete_service` / `test_rag_service` / `test_rag_answer_service` / `test_web_clip_service` / `test_plugin_service`

**数据层**（3 个）：
`test_document_repository` / `test_plugin_repository` / `test_plugin_isolation`

**组件**（4 个）：
`test_text_parser` / `test_chunker` / `test_file_storage` / `test_embedding_client` / `test_llm_client`

**安全**（2 个）：
`test_security` / `test_plugin_isolation`

## 项目结构

```
├── backend/
│   ├── main.py                    # FastAPI 工厂 + lifespan + 全局异常处理器
│   ├── api/
│   │   ├── deps.py                # get_current_plugin 身份依赖
│   │   └── routers/
│   │       ├── clips.py           # POST /clips
│   │       ├── documents.py       # 文档 CRUD + upload + ingest
│   │       ├── ingest.py          # POST /ingest/page
│   │       ├── plugins.py         # Plugin Workspace 6 端点
│   │       └── rag.py             # POST /rag/search + /rag/ask
│   ├── services/
│   │   ├── plugin_service.py      # 注册/认证/API Key/删除
│   │   ├── document_upload.py     # 上传 + 全链路编排
│   │   ├── document_ingest.py     # 生命周期 + 重试
│   │   ├── document_delete.py     # 幂等删除
│   │   ├── web_clip.py            # 网页剪藏
│   │   ├── ingest.py              # 底层 chunk 入库
│   │   ├── rag.py                 # RAG 语义检索
│   │   └── rag_answer.py          # 检索 + LLM 问答
│   ├── repositories/
│   │   ├── mysql/                 # documents + plugin_workspaces 仓储
│   │   └── milvus/                # 向量库仓储 + Initializer
│   ├── clients/
│   │   ├── embedding.py           # 百炼 Embedding 客户端
│   │   └── llm.py                 # 百炼 LLM 客户端（OpenAI 兼容）
│   ├── core/
│   │   ├── config.py              # Pydantic Settings 配置单源
│   │   ├── db.py                  # SQLAlchemy 引擎
│   │   ├── di.py                  # 依赖注入工厂
│   │   ├── exceptions.py          # 异常体系（Document/Plugin/Security/Milvus）
│   │   └── security.py            # AES-256-GCM 加密/解密
│   ├── models/                    # ORM + API Schema + Milvus DTO + Plugin ORM
│   ├── storage/                   # LocalFileStorage 本地文件存储
│   ├── parsers/                   # TextParser 文本解析
│   ├── chunkers/                  # RecursiveChunker 递归切块
│   └── tests/                     # 23 个测试文件、476 个用例
├── extension/                     # Chrome 扩展（MV3 Side Panel）
│   ├── manifest.json              # MV3 + sidePanel 权限
│   ├── background.js              # Service Worker
│   ├── sidepanel.html/js/css      # Side Panel 主界面
│   ├── content.js                 # 正文提取
│   ├── api-client.js              # HTTP 请求封装
│   ├── session-store.js           # Tab/Session 隔离存储
│   └── config.js                  # 后端地址配置
├── alembic/versions/              # 8 个数据库迁移（0001 → 0008）
├── docker-compose.yml             # MySQL / Redis / etcd / MinIO / Milvus
├── .env.example                   # 环境变量模板
└── docs/                          # 架构文档与历史设计文档
```

## 已知限制与演进方向

**当前边界**：

- 仅支持纯文本解析：`.txt` / `.md` / `.markdown`；`.pdf` / `.docx` / OCR 未实现
- ingest 为同步处理（请求内完成全部链路），无异步任务队列
- 无全局鉴权中间件：Plugin 身份仅在业务端点通过 `Depends(get_current_plugin)` 校验
- Redis 已部署但业务未使用
- 扩展正文提取为轻量 MVP：强 JS 渲染 / 分页 / paywall 页面可能提取不完整

**演进方向**：

- 扩展增强：正文提取升级、右键菜单剪藏、快捷键
- 异步 ingest 队列（Redis/Celery）+ 任务进度查询
- PDF / DOCX / OCR 解析接入
- 全局 API Token / JWT 鉴权中间件
- 基于对话历史的上下文连续问答
