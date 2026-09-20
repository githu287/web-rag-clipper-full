# Web RAG Clipper

Web RAG Clipper 是一个本地优先的网页剪藏与个人知识库 RAG 系统。Chrome/Edge 扩展负责采集网页、管理知识库和发起问答；FastAPI 后端负责文档解析、切块、向量化、检索及生成回答。

当前仓库已经打通以下链路：

- Plugin Workspace 注册与双凭证认证
- Workspace 级百炼 API Key 加密保存
- Side Panel 网页正文提取与剪藏前预览编辑，以及 `.txt` / `.md` / `.markdown` 文件上传
- 网页 URL 规范化与重复识别；锚点、默认端口和常见跟踪参数不会产生新文档
- Redis 异步网页/文件入库、持久化任务状态、进度轮询与失败重试
- MySQL 文档生命周期与 Milvus 向量索引
- 全知识库或指定文档的语义检索与 RAG 问答
- Side Panel 知识库、会话、设置和当前网页模式
- Workspace 间的文档、检索结果与会话隔离
- Retrieval 基线数据、运行脚本与报告渲染

## 系统组成

```text
Chrome / Edge Extension (Manifest V3)
  ├─ 网页正文提取、剪藏、文件上传
  ├─ 知识库列表、筛选、删除与失败重试
  └─ 当前文档 / 全知识库问答
                    │
                    │ HTTP + X-Plugin-ID / X-Plugin-Secret
                    ▼
FastAPI
  ├─ Plugin、Document、Clip、Ingest、RAG API
  ├─ Parser → Chunker → Embedding → Milvus
  ├─ Retrieval → Context → qwen-plus
  └─ MySQL 状态与归属校验
          │                         │
          ▼                         ▼
     MySQL 8.0                 Milvus 2.4.4
  元数据/状态/凭证              chunk/embedding
```

更完整的组件边界、数据流和一致性策略见 [ARCHITECTURE.md](ARCHITECTURE.md)，逐文件说明见 [FILE_INDEX.md](FILE_INDEX.md)。

## 技术栈

| 领域 | 实现 |
|---|---|
| API | Python 3.11、FastAPI、Uvicorn、Pydantic v2 |
| 关系数据 | MySQL 8.0、SQLAlchemy 2.0、PyMySQL、Alembic |
| 向量检索 | Milvus 2.4.4、pymilvus 2.4.15、HNSW + COSINE |
| 模型服务 | 阿里云百炼 OpenAI 兼容 API；`text-embedding-v3`、`qwen-plus` |
| 安全 | Plugin ID + Secret；Secret SHA-256；API Key AES-256-GCM |
| 浏览器端 | Chrome Extension Manifest V3、Side Panel、原生 JavaScript |
| 测试与评测 | pytest、150 条 Retrieval/隔离评测样本 |

Redis 用于异步入库队列和临时 payload；MySQL `ingest_jobs` 是任务状态的权威来源。

## 快速开始

### 1. 前置条件

- Docker Desktop 或兼容的 Docker Compose 环境
- Python 3.11
- 可用的阿里云百炼 API Key
- Chrome 或 Edge（加载浏览器扩展时需要）

### 2. 启动基础设施

```powershell
docker compose up -d
docker compose ps
```

Compose 会启动 MySQL、Redis、etcd、MinIO 和 Milvus。默认宿主端口如下：

| 服务 | 端口 |
|---|---:|
| MySQL | `33066` |
| Redis | `6379` |
| MinIO API / Console | `9000` / `9001` |
| Milvus gRPC / Health | `19530` / `9091` |

### 3. 配置后端

```powershell
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_hex(16))"
```

编辑 `.env`：

- 将 `MYSQL_PORT` 改为 `33066`，与本仓库的 Compose 映射一致。
- 将上一步生成的 32 个 ASCII 字符填入 `APP_MASTER_KEY`。
- 不要把真实密钥提交到 Git。

`BAILIAN_API_KEY` 是服务端兼容/预留配置。正常产品流程使用每个 Workspace 通过 `PUT /plugins/me/api-key` 保存的独立 Key。

### 4. 安装依赖并迁移数据库

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
alembic upgrade head
```

当前 Alembic head 为 `0009`。

### 5. 启动 API

```powershell
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

启动阶段会幂等初始化并加载 Milvus `page_chunks` Collection。打开 <http://localhost:8000/docs> 可查看和调用完整 API。

### 6. 启动异步 Worker

在另一个已激活虚拟环境的终端运行：

```powershell
python -m backend.workers.ingest_worker
```

Worker 会初始化 Milvus，恢复上次意外中断且未确认的任务，然后阻塞等待 Redis 队列。当前按单 Worker 模式设计。

### 7. 初始化 Workspace

在 Swagger UI 中按顺序调用：

1. `POST /plugins/register`，请求体为 `{"plugin_name":"My Workspace"}`。
2. 立即保存响应中的 `plugin_id` 和只返回一次的 `plugin_secret`。
3. 后续请求携带 `X-Plugin-ID` 与 `X-Plugin-Secret`。
4. 调用 `PUT /plugins/me/api-key`，请求体为 `{"api_key":"sk-..."}`。

后端会先验证 Key，再使用 `APP_MASTER_KEY` 进行 AES-256-GCM 加密。数据库不保存 Plugin Secret 或百炼 API Key 明文。

### 8. 加载扩展

1. 保持后端运行。
2. 打开 `chrome://extensions/` 或 `edge://extensions/`。
3. 开启开发者模式，选择“加载已解压的扩展程序”。
4. 选择仓库中的 `extension/` 目录。
5. 点击扩展图标打开 Side Panel，创建 Workspace、配置百炼 Key 后即可剪藏和问答。

若后端不在 `http://localhost:8000`，同时修改 `extension/config.js` 的 `API_BASE_URL` 与 `extension/manifest.json` 的 `host_permissions`。

## API 概览

仓库当前公开 20 个操作。只有 `POST /plugins/register` 无需认证；其余操作都要求 `X-Plugin-ID` 和 `X-Plugin-Secret`。

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/plugins/register` | 创建 Workspace，返回一次性明文 Secret |
| GET | `/plugins/me` | 获取当前 Workspace |
| PUT | `/plugins/me` | 修改 Workspace 名称 |
| PUT | `/plugins/me/api-key` | 验证并保存百炼 API Key |
| DELETE | `/plugins/me/api-key` | 清除百炼 API Key |
| DELETE | `/plugins/me` | 双重确认后级联删除 Workspace 资源 |
| GET | `/documents` | 分页列出文档；支持 keyword/status/source_type |
| POST | `/documents` | 创建 `PENDING` 文档元数据 |
| POST | `/documents/upload` | 上传并同步完成解析、切块与入库 |
| POST | `/documents/upload/async` | 安全落盘文件、创建入库任务并返回 `202` |
| GET | `/documents/{document_id}` | 获取当前 Workspace 的文档详情 |
| POST | `/documents/{document_id}/ingest` | 对已有文档执行或重试 ingest |
| DELETE | `/documents/{document_id}` | 幂等删除文档及关联资源 |
| POST | `/clips` | 剪藏网页正文；同 Workspace 同规范化 URL 可原位重试/更新 |
| POST | `/clips/async` | 创建网页剪藏任务并立即返回 `202` |
| GET | `/jobs/{job_id}` | 查询当前 Workspace 任务进度与结果 |
| POST | `/jobs/{job_id}/retry` | 重试未超过上限的失败任务 |
| POST | `/ingest/page` | 直接写入已切分 chunks 的底层接口 |
| POST | `/rag/search` | 返回语义检索结果及文档元数据 |
| POST | `/rag/ask` | 检索、构造 Context 并生成带 Sources 的回答 |

`/rag/search` 与 `/rag/ask` 均支持可选 `document_id`：省略时检索当前 Workspace 的全部成功文档，传入时只使用指定文档。跨 Workspace 访问统一表现为 404。

## 数据与状态

MySQL 是文档状态和归属的权威来源；Milvus 只保存向量检索所需字段。

- `plugin_workspaces`：Workspace 身份、Secret 哈希、加密后的百炼 Key、状态。
- `documents`：来源信息、文件元数据、Workspace 归属和生命周期状态。
- `ingest_jobs`：异步任务类型、进度、尝试次数、Document 结果和错误摘要。
- `page_chunks`：`id`、`page_id`、`chunk_index`、`chunk_text`、1024 维 `embedding`。
- 映射规则：`documents.id == page_chunks.page_id`，chunk 主键为 `{page_id}_{chunk_index}`。
- 生命周期：`PENDING → PROCESSING → SUCCESS | FAILED`；删除期间使用 `DELETING` 互斥。

检索会先按 Workspace 的成功文档 ID 构造 Milvus 表达式，返回后再回查 MySQL 并做一次归属和 `SUCCESS` 过滤。

## 配置

运行时真正由 `backend/core/config.py` 读取的主要变量如下：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MYSQL_HOST` / `MYSQL_PORT` | `localhost` / `3306` | Compose 场景需把端口改为 `33066` |
| `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DATABASE` | `rag_user` / 空 / `rag_clipper` | MySQL 连接信息 |
| `MILVUS_HOST` / `MILVUS_PORT` | `localhost` / `19530` | Milvus 连接信息 |
| `MILVUS_COLLECTION` | `page_chunks` | Collection 名称 |
| `APP_MASTER_KEY` | 空 | API Key 加密主密钥，必须恰好 32 字节 |
| `BAILIAN_BASE_URL` | 百炼兼容端点 | OpenAI 兼容 Base URL |
| `BAILIAN_EMBEDDING_MODEL` | `text-embedding-v3` | Embedding 模型 |
| `BAILIAN_EMBEDDING_DIMENSION` | `1024` | 必须与 Milvus Schema 一致 |
| `BAILIAN_LLM_MODEL` | `qwen-plus` | 问答模型 |
| `EMBEDDING_BATCH_SIZE` | `10` | 配置校验上限也是 10 |
| `UPLOAD_DIR` | `uploads` | 上传文件存储目录 |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `700` / `100` | 字符级递归切块参数 |
| `MAX_PAGE_CONTENT_BYTES` | `2097152` | 上传文件上限，2 MiB |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` | `localhost` / `6379` / `0` | 异步队列 Redis |
| `INGEST_QUEUE_NAME` | `web-rag:ingest` | 待处理任务队列名 |
| `INGEST_PAYLOAD_TTL_SECONDS` | `604800` | Redis 任务 payload 保留时间 |
| `INGEST_MAX_RETRIES` | `3` | 失败任务最大尝试次数 |

`.env.example` 中还有 API、Redis、重试和 CORS 等预留字段；当前 Settings 或业务代码并未消费其中全部字段。后端目前也未注册 CORS 中间件。

## 测试

当前已接入的主测试集：

```powershell
.venv\Scripts\python.exe -m pytest -q `
  --ignore=backend/tests/test_auth_api.py `
  --ignore=backend/tests/test_user_repository.py `
  --ignore=backend/tests/test_user_service.py
```

当前活动测试集结果为：`550 passed, 37 subtests passed`。

仓库工作区中另有尚未接入主应用的旧 User/Bearer 迁移草稿（`auth.py`、`users.py`、`user_*` 及对应三个测试文件）。直接运行不带 ignore 的全量 `pytest` 会在这三个测试模块的收集阶段失败；当前产品身份模型以 Plugin Workspace 为准。

## Retrieval 评测

`evaluation/` 包含：

- 70 条主 RAG 样本
- 10 条负例/拒答样本
- 70 条 Workspace 隔离样本
- 两个 Workspace、共 40 篇源文档
- 数据校验、运行时 ID 对齐、基线执行和 Markdown 报告渲染工具

当前公开数据的 chunk 标注仍有部分需要人工重标，因此应把现有结果视为文档级临时基线，不应宣称为完整 chunk 级基线。具体流程见 [evaluation/README.md](evaluation/README.md) 与 [docs/REAL_BASELINE_RUNBOOK.md](docs/REAL_BASELINE_RUNBOOK.md)。

## 已知限制

- 扩展的网页剪藏和文件上传已使用 Redis Worker 异步入库；旧 `/clips` 与 `/documents/upload` 兼容接口仍是同步。
- 当前 Worker 是单进程模式；启动时会恢复 Redis processing 列表中未确认的任务。
- 仅解析 UTF-8 文本和 Markdown；PDF、DOCX、OCR 尚未接入。
- 扩展正文提取是 DOM 启发式实现，复杂 SPA、分页、登录墙可能提取不完整。
- 会话历史保存在浏览器 `chrome.storage.local`，后端没有会话表。
- Milvus Collection 已存在时初始化器不会自动迁移 Schema；修改向量维度需重建并重新 ingest。
- 文档删除和 Workspace 删除通过有序、可重试的补偿流程实现，不是跨 MySQL/Milvus/文件系统的分布式事务。
- 当前没有限流、审计、生产级 Secret 轮换或公网部署加固。

## 目录

```text
backend/       FastAPI、业务服务、Repository、模型与测试
extension/     Chrome/Edge Manifest V3 扩展
alembic/       MySQL Schema 迁移（0001 → 0009）
evaluation/    Retrieval/隔离评测数据与工具
docs/          历史设计、数据模型和运行手册
uploads/       本地运行时上传目录（Git 忽略）
```
