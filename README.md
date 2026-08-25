# Web RAG Clipper

面向网页内容剪藏与知识库构建的 RAG 系统。

**当前阶段**：已完成 RAG 后端核心链路（文档上传、Web Clip 网页剪藏、生命周期管理、向量化入库、RAG 检索），并已实现 Chrome Browser Extension（Manifest V3）第一版（网页正文采集 + 一键剪藏）。

后端链路：文档/网页剪藏进入后，系统自动完成 **切块 → 向量化 → 向量入库**，并提供基于语义相似度的 **RAG 检索** 能力（检索结果携带文档来源与元数据）。

## 功能特性

**产品定位**：Web RAG Clipper 通过浏览器扩展采集网页内容，送入后端知识库完成切分、向量化和 RAG 检索。后端核心链路已实现，浏览器扩展端（`extension/`，Manifest V3）第一版已实现：采集当前网页标题/URL/正文 → `POST /clips` 剪藏 → RAG 检索。

当前阶段已实现：

- 文档全链路自动化：上传文件后同步完成解析、切块、Embedding、向量入库，无需人工介入
- Web Clip 网页剪藏：`POST /clips` 接收网页 URL/标题/正文，`source_type=webpage` 固定，不创建物理文件直接入库
- Browser Extension（MV3）：Chrome 插件一键剪藏当前网页，正文提取（article→main→body）+ 剪藏结果反馈
- RAG 语义检索：Milvus 向量检索 + MySQL 文档状态过滤，返回检索片段与文档元数据（`document_id` / `filename` / `created_at` / `title` / `url` / `source_type`）
- 文档生命周期管理：`PENDING → PROCESSING → SUCCESS / FAILED`，失败可重试，删除幂等（204）
- 三方一致性设计：MySQL `documents` 表为状态权威，Milvus `page_chunks` 为向量数据载体，`document.id == Milvus.page_id` 1:1 映射
- 完整测试覆盖：15 个测试文件、239 个单元测试用例全部通过（Phase 3.1 实测基线）
- Docker Compose 一键启动基础设施（MySQL / Redis / etcd / MinIO / Milvus）

## 技术栈

| 类别 | 技术 | 说明 |
|---|---|---|
| 语言 | Python 3.11.9 | |
| Web 框架 | FastAPI + Uvicorn | 应用版本 `0.1.0` |
| 数据校验 | Pydantic v2 | 全部 Schema 使用 `extra="forbid"` 防契约漂移 |
| MySQL | MySQL 8.0 + SQLAlchemy 2.0 + PyMySQL | `documents` 表（状态权威） |
| 迁移 | Alembic | head = `0003` |
| 向量库 | Milvus v2.4.4 (standalone) + pymilvus==2.4.15 | `page_chunks` Collection（版本锁死，禁止跨 minor 升级） |
| Embedding | 阿里云百炼 `text-embedding-v3` | 1024 维，批量上限 10 条/请求 |
| LLM | 百炼 `qwen-plus` | 已配置，当前未接入业务 |
| Redis | redis 7-alpine | compose 已部署，当前业务未使用（预留） |
| 测试 | unittest | 无第三方测试框架依赖 |

## 系统架构

```
[Browser Extension]（已实现，MV3）
    │  正文采集(article→main→body) / POST /clips 剪藏
    ▼
[FastAPI Backend]（当前已实现）
    │  multipart 上传 / JSON 请求
    ▼
┌─────────────────────────────────────────────┐
│  API 层（FastAPI Router，依赖注入装配）       │
│  POST /documents/upload                     │
│  POST /documents/{id}/ingest                │
│  POST /documents · DELETE /documents/{id}   │
│  POST /ingest/page · POST /rag/search       │
└──────────────────┬──────────────────────────┘
                   ▼
┌─────────────────────────────────────────────┐
│  Service 层                                  │
│  DocumentUploadService  → 上传 + 全链路编排   │
│  DocumentIngestService  → 生命周期 + 重试     │
│  DocumentDeleteService  → 幂等删除           │
│  RagService             → RAG 检索           │
└───┬────────┬────────┬────────┬──────────────┘
    ▼        ▼        ▼        ▼
 存储/解析/切分/向量化/双存储
 LocalFileStorage ──► uploads/ 目录（原始文件）
 TextParser ────────► .txt / .md / .markdown
 RecursiveChunker ──► 700 字符 / 100 重叠
 Bailian Embedding ─► text-embedding-v3 (1024d)
 MySQL documents ───► 文档状态权威
 Milvus page_chunks ► 向量检索（HNSW + COSINE）
                    │
                    ▼
              RAG Search（Top-K + SUCCESS 过滤 + 来源元数据）
```

核心链路（当前已实现）：

```
上传文档 → LocalFileStorage 落盘 → 创建 Document(PENDING)
  → TextParser 解析 → RecursiveChunker 切块
  → 百炼 Embedding → Milvus 写入 chunks → Document 置 SUCCESS
  → RAG Search（相似度 Top-K + SUCCESS 过滤 + 来源元数据）
```

## 快速开始

### 前置要求

- Docker Desktop（Windows 需保持运行）
- Python 3.11+（建议 3.11.9）
- 阿里云百炼 API Key（`text-embedding-v3` 访问凭证）

### 1. 启动基础设施

```powershell
docker compose up -d
```

启动的服务：MySQL(宿主 `33066`)、Redis(`6379`)、etcd(`2379`)、MinIO(`9000`)、Milvus(`19530`)。

> 注意：MySQL 通过宿主 `33066` 暴露（容器内 `3306`）。若你的环境可释放 `3306`，可改回 `3306:3306` 并同步 `.env` 的 `MYSQL_PORT`。

### 2. 配置环境变量

```powershell
copy .env.example .env
```

编辑 `.env`，至少修改：

- `BAILIAN_API_KEY`：填写百炼 API Key
- `MYSQL_PORT`：使用本 compose 时填 `33066`

### 3. 安装依赖（项目虚拟环境）

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r backend\requirements.txt
```

### 4. 初始化数据库表

```powershell
alembic upgrade head
```

### 5. 启动 API

```powershell
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

启动时 lifespan 会自动初始化 Milvus Collection（幂等：已存在则跳过）。

### 6. 验证

- 打开 http://localhost:8000/docs （Swagger UI，可直接调试全部 7 个接口）
- 上传一个 `.txt` 文件测试全链路：

```powershell
curl.exe -F "file=@C:\path\to\sample.txt" http://localhost:8000/documents/upload
```

## Chrome 扩展（Browser Extension）

第一版已实现（Manifest V3），文件位于 `extension/`：

- `manifest.json`：MV3 声明，权限最小化（`activeTab` + `scripting`；`host_permissions` 仅后端 `http://localhost:8000/*`）
- `config.js`：后端地址集中配置（`API_BASE_URL`），其他文件不散落硬编码
- `content.js`：正文提取（`article → main → body`，清理 script/style/nav/footer/header/iframe/广告区，空白归一化）
- `popup.html` / `popup.js`：剪藏 UI、向 content script 请求正文、`POST /clips` 调用与结果反馈

### 加载方式

1. 启动后端：`uvicorn backend.main:app --host 0.0.0.0 --port 8000`
2. Chrome 打开 `chrome://extensions/`，开启右上角 **Developer mode**
3. 点击 **Load unpacked**，选择本项目 `extension/` 目录
4. 打开任意 http/https 网页，点击工具栏插件图标
5. 确认页面标题 / URL / 正文长度 → 点击 **剪藏当前页面**
6. 剪藏成功显示 Document ID 与 chunk_count；可用 `POST /rag/search` 检索该网页内容

### 说明

- 扩展仅在用户点击插件时对当前 tab 注入脚本（`activeTab` 权限），不在所有网页后台常驻，权限最小化
- 扩展只负责「网页采集 + 调 API + UI」，向量化 / 入库 / 检索全部由后端完成
- 后端地址统一在 `extension/config.js` 修改；修改后需同步 `manifest.json` 的 `host_permissions`

## API 一览

共 7 个端点（无 `/api` 前缀；`POST /ingest/page` 为底层 chunk 入库接口，面向生命周期链路复用）。

| 方法 | 路径 | 说明 | 成功状态码 |
|---|---|---|---|
| POST | `/documents/upload` | multipart 上传文档并完整入库（解析→切块→向量化→入库→SUCCESS） | 201 |
| POST | `/clips` | Web Clip 网页剪藏（`source_type=webpage`，正文直接入库不落盘） | 201 |
| POST | `/documents` | 创建 Document 元数据（`status=PENDING, chunk_count=0`） | 201 |
| POST | `/documents/{document_id}/ingest` | Document 生命周期 ingest；`FAILED` 文档可直接重试（`FAILED → PROCESSING → SUCCESS/FAILED`） | 200 |
| DELETE | `/documents/{document_id}` | 删除文档（幂等：不存在也返回 204；顺序 Milvus → 文件 → MySQL） | 204 |
| POST | `/ingest/page` | 页面 chunk 入库（底层 re-ingest：query old → upsert new → delete stale） | 200 |
| POST | `/rag/search` | RAG 语义检索（候选 `max(limit,10)` → SUCCESS 状态过滤 → top-K） | 200 |

### 典型请求/响应

**上传文档** `POST /documents/upload`

```json
// 201 Created
{
  "id": 1,
  "filename": "sample.txt",
  "file_size": 2048,
  "mime_type": "text/plain",
  "status": "SUCCESS",
  "chunk_count": 6,
  "error_message": null
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

> `document_id == page_id`（1:1 映射）；`distance` 为 COSINE similarity（越大越相似，1.0 为完全相似）；结果按相似度降序。

### 错误映射

| HTTP | 场景 |
|---|---|
| 400 | 空文件、文件名非法、路径穿越（防御）、解析不支持（防御） |
| 404 | Document 不存在 |
| 413 | 文件超过大小上限（默认 2 MB，`MAX_PAGE_CONTENT_BYTES`） |
| 415 | 不支持的扩展名（当前仅 `.txt` / `.md` / `.markdown`；`.pdf` / `.docx` 明确拒绝） |
| 500 | 存储 / 解析 / 切分内部错误 |
| 502 | 百炼 Embedding 服务异常（可重试） |
| 503 | Milvus 或 MySQL 数据服务异常（可重试） |

## 环境变量

全部配置项见 `.env.example`（复制为 `.env` 后填写，禁止硬编码 API Key）。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `APP_ENV` | `development` | 运行环境 |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` | 服务监听地址 / 端口 |
| `API_TOKEN` | `change-me...` | 预留鉴权令牌（当前实现未接入） |
| `MYSQL_HOST` / `MYSQL_PORT` | `localhost` / `3306` | MySQL 连接（compose 场景端口填 `33066`） |
| `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DATABASE` | `rag_user` / `rag_password` / `rag_clipper` | MySQL 凭证 |
| `REDIS_*` | — | Redis 连接（当前业务未使用） |
| `MILVUS_HOST` / `MILVUS_PORT` | `localhost` / `19530` | Milvus 连接 |
| `MILVUS_COLLECTION` | `page_chunks` | Milvus Collection 名 |
| `BAILIAN_API_KEY` | 空 | 百炼 API Key（必填） |
| `BAILIAN_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 百炼 OpenAI 兼容端点 |
| `BAILIAN_EMBEDDING_MODEL` | `text-embedding-v3` | Embedding 模型 |
| `BAILIAN_EMBEDDING_DIMENSION` | `1024` | 向量维度（必须与 Milvus `embedding.dim` 一致，修改需重建 Collection） |
| `BAILIAN_LLM_MODEL` | `qwen-plus` | LLM 模型（预留） |
| `UPLOAD_DIR` | `uploads` | 原始文件存储根目录 |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `700` / `100` | 递归切块参数 |
| `EMBEDDING_BATCH_SIZE` | `10` | Embedding 批量上限（百炼硬限制 10，勿超过） |
| `MAX_PAGE_CONTENT_BYTES` | `2097152` | 上传文件大小上限（2 MB） |
| `RAG_TOP_K` | `5` | 预留配置（当前不读取；RAG limit 由 API 参数决定） |
| `INGEST_MAX_RETRIES` | `3` | 预留配置（当前不读取） |
| `PROCESSING_TIMEOUT_SECONDS` | `600` | 预留配置（当前不读取） |
| `CORS_ORIGINS` | `chrome-extension://*` | 预留 CORS 配置（当前实现未接入中间件） |

## 数据模型

### MySQL `documents` 表（状态权威，11 字段）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INT PK AUTO_INCREMENT | Document 主键，1:1 对应 Milvus `page_id` |
| `user_id` | INT NULL | 所属用户（当前无 user 体系，恒为 NULL） |
| `filename` | VARCHAR(255) | 文件名 |
| `file_path` | VARCHAR(512) | 文件存储路径 |
| `status` | VARCHAR(32) | `PENDING / PROCESSING / SUCCESS / FAILED / DELETING` |
| `chunk_count` | INT | 已入库 chunk 数 |
| `created_at` | DATETIME | 创建时间 |
| `updated_at` | DATETIME | 更新时间（ON UPDATE） |
| `file_size` | INT | 文件字节数 |
| `mime_type` | VARCHAR(128) | MIME 类型 |
| `error_message` | TEXT NULL | 失败摘要（截断至 2048 字符） |

### Milvus `page_chunks` Collection（5 字段）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | VARCHAR(64) PK | `{page_id}_{chunk_index}` |
| `page_id` | INT64 | = `documents.id`（1:1） |
| `chunk_index` | INT64 | chunk 序号（0 起） |
| `chunk_text` | VARCHAR(4096) | 切块文本 |
| `embedding` | FLOAT_VECTOR(1024) | 百炼向量 |

- 向量索引：`HNSW`（metric=COSINE，M=16，efConstruction=200）；`page_id` 建 `INVERTED` 索引
- 检索参数：`ef=128`，返回 `id/page_id/chunk_index/chunk_text`，不返回 `embedding`
- 初始化幂等：应用启动时 `MilvusInitializer.initialize()`，Collection 已存在则跳过，不删重建

## 测试

测试框架：`unittest`（无第三方测试依赖），测试文件位于 `backend/tests/`。

```powershell
.venv\Scripts\python.exe -m unittest discover -s backend/tests
```

覆盖范围（15 个测试文件）：

- API 层：`test_document_api.py` / `test_document_upload_api.py` / `test_ingest_api.py` / `test_rag_api.py` / `test_web_clip_api.py`
- Service 层：`test_document_upload_service.py` / `test_document_ingest_service.py` / `test_document_delete_service.py` / `test_rag_service.py` / `test_web_clip_service.py`
- 数据层：`test_document_repository.py`
- 组件：`test_text_parser.py` / `test_chunker.py` / `test_file_storage.py` / `test_embedding_client.py`

> 实测基线：239 个测试用例全部通过（Phase 3.1.3 核对：201 + 38 = 239）。

## 项目结构

```
├── backend/
│   ├── main.py                 # FastAPI 应用工厂 + lifespan + 全局异常处理器
│   ├── api/routers/            # documents / ingest / rag 三个 Router
│   ├── services/               # upload / ingest / delete / rag 业务编排
│   ├── repositories/           # MySQL 与 Milvus 仓储（Protocol + Impl）
│   ├── storage/                # LocalFileStorage 本地文件存储
│   ├── parsers/                # TextParser 文本解析
│   ├── chunkers/               # RecursiveChunker 递归切块
│   ├── clients/                # 百炼 Embedding 客户端
│   ├── models/                 # ORM / API Schema / Milvus DTO
│   ├── core/                   # 配置 / 数据库 / DI / 异常体系
│   └── tests/                  # 239 个单元测试
├── docs/                       # 历史设计文档（PHASE*）与当前实现架构文档
├── extension/                  # Chrome 扩展（MV3）：manifest / content / popup / config
├── alembic/                    # 数据库迁移（head = 0003）
├── docker-compose.yml          # MySQL / Redis / etcd / MinIO / Milvus
└── .env.example                # 环境变量模板
```

## 已知限制与路线图

**当前已实现边界（如实说明）：**

- 仅支持纯文本解析：`.txt` / `.md` / `.markdown`；`.pdf` / `.docx` / OCR / MinerU 未实现
- ingest 为同步处理（上传请求内完成全部链路），无异步任务队列
- 无鉴权体系：`API_TOKEN` / CORS 中间件已预留配置但未接入（本地开发可接受；部署到局域网/公网前必须实现 API Token / JWT 校验）
- 无 user / 多租户体系（`user_id` 字段预留）
- Redis 已部署但业务未使用；`RAG_TOP_K` / `INGEST_MAX_RETRIES` / `PROCESSING_TIMEOUT_SECONDS` 为预留配置
- 扩展正文提取为轻量 MVP：`article → main → body` + 噪声节点清理；强 JS 渲染 / 分页 / paywall 页面可能提取不完整；`raw_text` 当前无长度上限（建议后续与上传 2 MB 上限对齐）

**演进方向（未实现，不承诺）：**

- 扩展增强：正文提取升级、剪藏历史列表、右键菜单剪藏、快捷键
- 异步 ingest 队列（Redis/Celery）+ 任务进度查询
- PDF/DOCX/OCR 解析接入
- 鉴权（API Token / JWT）与多租户隔离
- 基于 `qwen-plus` 的生成式问答（当前仅检索，不含 LLM 生成）
