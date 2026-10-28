# Web RAG Clipper

Web RAG Clipper 是一个本地优先、以网页剪藏为核心的个人知识库 RAG 系统。Chrome/Edge 扩展负责提取网页正文、管理知识库和发起问答；FastAPI 后端负责异步入库、切块、向量化、检索与回答生成。

项目的首要场景是“浏览网页时剪藏，稍后基于已剪藏内容提问”。文本和 Markdown 上传是辅助能力，PDF、DOCX 与 OCR 暂不作为当前重点。

当前仓库已经打通以下链路：

- Plugin Workspace 注册与双凭证认证
- Workspace 级多模型服务配置：Embedding 与问答模型可分别选择服务商，API Key 加密保存
- 网页正文候选评分与噪声清理，保留标题、列表、引用、代码块和表格结构
- Side Panel 四种剪藏方式（智能正文、整页可读内容、选中文字、指定区域）、预览编辑、提取质量诊断，以及 `.txt` / `.md` / `.markdown` 文件上传
- 网页 URL 规范化与重复识别；锚点、默认端口和常见跟踪参数不会产生新文档，哈希路由站点（`#/...`）的每个路由各算一篇
- Redis 异步网页/文件入库、持久化任务状态、进度轮询与失败重试
- MySQL 文档生命周期与 Milvus 向量索引
- 全知识库或指定文档的语义检索与 RAG 问答
- Side Panel 知识库、会话、设置和当前网页模式
- Workspace 间的文档、检索结果与会话隔离
- Retrieval 基线数据、运行脚本与报告渲染

## 系统组成

```text
Chrome / Edge Extension (Manifest V3)
  ├─ 网页正文提取、预览编辑与剪藏
  ├─ 知识库列表、筛选、删除与失败重试
  └─ 当前文档 / 全知识库问答
                    │
                    │ HTTP + X-Plugin-ID / X-Plugin-Secret
                    ▼
FastAPI
  ├─ Plugin、Document、Clip、Ingest、RAG API
  ├─ MySQL Job → Redis Queue → Ingest Worker
  ├─ Parser → Chunker → Embedding API → Milvus
  ├─ Retrieval → Context → LLM API
  └─ MySQL 状态与归属校验
          │              │              │
          ▼              ▼              ▼
     MySQL 8.0       Redis 7       Milvus 2.4.4
  元数据/任务/凭证    异步队列      chunk/embedding
```

更完整的组件边界、数据流和一致性策略见 [ARCHITECTURE.md](ARCHITECTURE.md)，逐文件说明见 [FILE_INDEX.md](FILE_INDEX.md)。

## 技术栈

| 领域 | 实现 |
|---|---|
| API | Python 3.11、FastAPI、Uvicorn、Pydantic v2 |
| 关系数据 | MySQL 8.0、SQLAlchemy 2.0、PyMySQL、Alembic |
| 向量检索 | Milvus 2.4.4、pymilvus 2.4.15、HNSW + COSINE |
| 模型服务 | OpenAI 兼容 API；百炼、OpenAI、Gemini、DeepSeek、硅基流动、OpenRouter、自定义 HTTPS 端点 |
| 安全 | Plugin ID + Secret；Secret SHA-256；API Key AES-256-GCM |
| 浏览器端 | Chrome Extension Manifest V3、Side Panel、原生 JavaScript |
| 测试与评测 | pytest、150 条 Retrieval/隔离评测样本 |

Redis 用于异步入库队列和临时 payload；MySQL `ingest_jobs` 同时保存任务状态与可重建投递的 payload。Redis 暂时不可用时，已提交任务仍返回 `202` 并保持排队，Worker 定期幂等补投；成功后清除正文 payload，失败网页正文只保留到首次提交时确定的期限（默认 7 天），重试不续期，过期后由 Worker 分批清理。上传任务可从 Document 的文件引用重建。payload 不含模型凭证。

### 支持的模型服务

Embedding 与 LLM 独立配置，因此可以使用“OpenAI Embedding + DeepSeek 问答”等组合。

| 服务商 | Embedding | LLM |
|---|:---:|:---:|
| 阿里云百炼 | ✓ | ✓ |
| OpenAI | ✓ | ✓ |
| Google Gemini | ✓ | ✓ |
| 硅基流动 | ✓ | ✓ |
| DeepSeek | — | ✓ |
| OpenRouter | — | ✓ |
| 自定义 OpenAI 兼容 HTTPS 端点 | ✓ | ✓ |

预设服务商的官方 Base URL 由后端锁定，客户端不能覆盖；自定义端点必须使用不含凭据、查询串和 fragment 的 HTTPS URL。模型名称可以按服务商实际开放的模型修改。Milvus Schema 固定为 1024 维，因此所选 Embedding 模型必须能返回 1024 维向量。

为防止不同模型的向量在同一知识库中混用，Workspace 已有文档时不能直接更换 Embedding 服务商或模型；更换同一 Embedding 配置的 Key、或更换 LLM 不受影响。确需切换 Embedding 时，应先删除当前 Workspace 的旧文档，再保存新配置并重新剪藏。

## 快速开始

### 1. 前置条件

- Docker Desktop 或兼容的 Docker Compose 环境
- Python 3.11
- 至少一个可用的 Embedding API Key 和一个可用的问答模型 API Key；若服务商同时支持二者，可填写同一个 Key
- Chrome 或 Edge（加载浏览器扩展时需要）

### 2. 启动基础设施

```powershell
docker compose up -d
docker compose ps
```

Compose 会启动 MySQL、Redis、etcd、MinIO 和 Milvus。默认宿主端口如下：

Compose 会自动合并 `docker-compose.override.yml`，沿用远端开发环境固定版本和摘要的 PGSTY MinIO 分支镜像。本次代码整合未验证镜像拉取或启动服务；开发步骤见 [LOCAL_DEVELOPMENT.md](LOCAL_DEVELOPMENT.md)。

| 服务 | 端口 |
|---|---:|
| MySQL | `33066` |
| Redis | `16379` |
| MinIO API / Console | `19000` / `19001` |
| Milvus gRPC / Health | `19530` / `9091` |

### 3. 配置后端

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
python -c "import secrets; print(secrets.token_hex(16))"
```

编辑 `.env`：

- 将 `MYSQL_PORT` 改为 `33066`、`REDIS_PORT` 改为 `16379`，与本仓库的 Compose 映射一致。
- 将上一步生成的 32 个 ASCII 字符填入 `APP_MASTER_KEY`。
- 不要把真实密钥提交到 Git。

`APP_MASTER_KEY` 不是模型 API Key，而是后端用于加密 Workspace 模型凭证的主密钥。报错 `must be exactly 32 bytes` 表示它未配置或 UTF-8 编码后并非恰好 32 字节。

已有环境请保留原 `.env` 和 `APP_MASTER_KEY`，不要用新生成的值替换，否则已有模型配置将无法解密。

`BAILIAN_API_KEY` 是旧流程的兼容/预留配置。当前产品流程通过扩展设置页或 `PUT /plugins/me/model-config` 保存每个 Workspace 自己的 Embedding 与 LLM 凭证。

### 4. 安装依赖并迁移数据库

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
alembic upgrade head
```

当前 Alembic head 为 `0012`。保留 GitHub 已发布的 `0011`（持久化 payload 与排队扫描索引），`0012` 增加网页正文过期时间与索引。在线升级兼容远端 `0011` 和此前本地已含过期字段的 `0011`：保留已有期限；缺失期限的历史网页正文按创建时间加 7 天回填，较旧正文可能立即过期。升级时先停止 API/Worker，迁移后再启动。离线 SQL 以标准远端 `0011` 为起点。旧网页任务若正文已经丢失，无法由迁移恢复。整合决策和验证范围见 [VERSION_COMPARISON.md](VERSION_COMPARISON.md)。

### 5. 启动 API

```powershell
python -m uvicorn backend.main:app --host 127.0.0.1 --port 18001
```

启动阶段会幂等初始化并加载 Milvus `page_chunks` Collection。打开 <http://localhost:18001/docs> 可查看和调用完整 API。

### 6. 启动异步 Worker

在另一个已激活虚拟环境的终端运行：

```powershell
python -m backend.workers.ingest_worker
```

Worker 会初始化 Milvus，恢复上次意外中断且未确认的任务，并每约 5 秒扫描 MySQL 排队任务、幂等补投 Redis。任务执行期间该扫描等待本次任务结束。当前按单 Worker 模式设计。

停机方式有区别：`SIGTERM`（`systemctl stop`、`docker stop`、`kill <pid>`）会等当前任务收尾后再退出；Ctrl+C（`SIGINT`）仍是立即中断。下次启动和任务边界会同时检查 MySQL RUNNING 与 Redis processing：已成功文档补记任务成功，可重试的任务重新排队，超过次数上限的任务记为失败。

### 7. 初始化 Workspace

在 Swagger UI 中按顺序调用：

1. `POST /plugins/register`，请求体为 `{"plugin_name":"My Workspace"}`。
2. 立即保存响应中的 `plugin_id` 和只返回一次的 `plugin_secret`。
3. 后续请求携带 `X-Plugin-ID` 与 `X-Plugin-Secret`。
4. 调用 `GET /plugins/model-providers` 查看内置服务商，或直接在扩展设置页选择服务商。
5. 调用 `PUT /plugins/me/model-config`，分别提交 Embedding 与 LLM 的服务商、模型和 API Key。

后端会分别验证 Embedding 与 LLM 配置，再使用 `APP_MASTER_KEY` 对完整配置做 AES-256-GCM 加密。数据库不保存 Plugin Secret 或模型 API Key 明文。旧的单百炼 Key 会自动按百炼 Embedding + 百炼 LLM 继续读取，无需手工迁移。

### 8. 加载扩展

1. 保持后端运行。
2. 打开 `chrome://extensions/` 或 `edge://extensions/`。
3. 开启开发者模式，选择“加载已解压的扩展程序”。
4. 选择仓库中的 `extension/` 目录。
5. 点击扩展图标打开 Side Panel，创建 Workspace，在“模型配置”中分别选择 Embedding 与问答服务后即可剪藏和问答。

若后端不在 `http://localhost:18001`，同时修改 `extension/config.js` 的 `API_BASE_URL` 与 `extension/manifest.json` 的 `host_permissions`。

## 使用流程

1. 打开普通 HTTP(S) 网页，在扩展 Side Panel 中选择剪藏方式，点击“提取并预览”。
2. 检查标题、正文和提取诊断；必要时手工删除导航、广告等内容。
3. 确认剪藏后，API 创建任务，Worker 完成切块、Embedding 和 Milvus 入库。
4. 在“当前网页”模式只检索这篇文档，或切换“全部知识库”跨文档提问。
5. 在“我的知识库”查看状态、重试失败任务或删除文档。

上传接口在进入业务落盘前按上限加 1 字节进行有界读取，超限返回 413；同步和异步上传接口均在线程池运行。此限制针对业务读取，不代表 ASGI/multipart 解析层的总请求体限制。文档和 Workspace 删除也将同步数据库、文件及向量调用移入线程，避免阻塞事件循环。

| 剪藏方式 | 用法 |
|---|---|
| 智能正文 | 默认模式，按正文容器评分清理导航、广告和推荐；无可靠候选时尝试本地 Readability，再降级提取 |
| 整页可读内容 | 保留已加载的整页文字，包括导航和推荐；适合列表、问答和非文章页面，保存前检查预览 |
| 选中文字 | 先在网页选中文字，再点击提取；只保留选中范围，支持有权限访问的可见 iframe |
| 指定区域 | 点击提取后在网页移动鼠标查看高亮，点击选定区域；↑ 扩大范围、Esc 或侧栏按钮取消 |

默认提取最多等待约 3 秒让内容稳定。0.9.0 增加可选的“加载更多后提取”，适用于智能正文/整页内容：从页面或主要滚动容器顶部逐段滚动并采集，最多 20 次、滚动预算 25 秒；侧栏显示进度，支持取消按钮或 Esc。完成、取消和失败后恢复原阅读位置；达到次数、时间或字符限制时保留已采集内容，提醒可能不完整。虚拟列表采用相邻片段重叠合并，无法可靠对齐时提醒检查重复/遗漏。不会自动点击“展开全文”或翻页，滚动触发的站点加载行为仍受网站自身权限约束。

提取可见、可访问的 HTTP(S) iframe，合并结果时标注内嵌来源并去重，文档仍绑定顶层网页 URL。自动滚动只处理顶层页面的主要区域，子框架沿用普通提取；滚动中新加载的框架可能遗漏并会提示。最多探测 64 个框架并处理其中 12 个（含顶层）；跨域需要浏览器授予对应站点权限，权限受限或数量超限会显示诊断提醒。

四种方式共享 50 万字符上限，保存前均可编辑。同一规范化 URL 再次保存会更新已有文档，选区与区域不会自动创建多个独立摘录。图片仅保留替代说明；不下载图片、不执行 OCR，图片为主的短文章会提醒。支持部分开放 Shadow DOM 与插槽；关闭的组件、浏览器内部页、扩展商店页、未登录/未购买的正文、PDF/Canvas、虚拟列表及未加载的后续内容仍有边界。0.8.1 已完成 [13 个公开网页样本实测](SITE_AUDIT_2026-10-28.md) 并修复正文范围、标题和代码保真问题；知乎/CSDN 样本受访问限制，没有据此宣称全站兼容。能力说明见 [剪藏能力与验收](CLIPPING_COMPATIBILITY.md)。

## API 概览

仓库当前注册 24 个操作。`POST /plugins/register` 和 `GET /plugins/model-providers` 无需认证；其余操作都要求 `X-Plugin-ID` 和 `X-Plugin-Secret`。

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/plugins/register` | 创建 Workspace，返回一次性明文 Secret |
| GET | `/plugins/me` | 获取当前 Workspace |
| PUT | `/plugins/me` | 修改 Workspace 名称 |
| GET | `/plugins/model-providers` | 获取内置 Embedding/LLM 服务商预设 |
| GET | `/plugins/me/model-config` | 获取不含 Key 的当前模型配置摘要 |
| PUT | `/plugins/me/model-config` | 分别验证并保存 Embedding 与 LLM 配置 |
| PUT | `/plugins/me/api-key` | 旧版兼容：保存单个百炼 API Key |
| DELETE | `/plugins/me/api-key` | 清除模型配置 |
| DELETE | `/plugins/me` | 双重确认后级联删除 Workspace 资源 |
| GET | `/documents` | 分页列出文档；支持 keyword/status/source_type |
| POST | `/documents` | 创建 `PENDING` 虚拟文档元数据；`file_path` 省略或为空，不接受本地路径 |
| POST | `/documents/upload` | 上传并同步完成解析、切块与入库 |
| POST | `/documents/upload/async` | 安全落盘文件、创建入库任务并返回 `202` |
| GET | `/documents/{document_id}` | 获取当前 Workspace 的文档详情 |
| GET | `/documents/{document_id}/job` | 查找文档的进行中或最近入库任务；不依赖浏览器本地映射 |
| POST | `/documents/{document_id}/ingest` | 对已有文档执行或重试 ingest |
| DELETE | `/documents/{document_id}` | 幂等删除文档及关联资源 |
| POST | `/clips` | 剪藏网页正文；同 Workspace 同规范化 URL 可原位重试/更新 |
| POST | `/clips/async` | 创建网页剪藏任务并立即返回 `202` |
| GET | `/jobs/{job_id}` | 查询当前 Workspace 任务进度与结果 |
| POST | `/jobs/{job_id}/retry` | 重试未超过上限的失败任务 |
| POST | `/ingest/page` | 向当前 Workspace 的已有文档写入 chunks，校验归属并更新文档状态 |
| POST | `/rag/search` | 返回语义检索结果及文档元数据 |
| POST | `/rag/ask` | 检索、构造 Context 并生成带 Sources 的回答 |

`/rag/search` 与 `/rag/ask` 均支持可选 `document_id`：省略时检索当前 Workspace 的全部成功文档，传入时只使用指定文档。跨 Workspace 访问统一表现为 404。

## 数据与状态

MySQL 是文档状态和归属的权威来源；Milvus 只保存向量检索所需字段。

- `plugin_workspaces`：Workspace 身份、Secret 哈希、加密后的模型配置、Embedding 指纹和状态。
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
| `BAILIAN_BASE_URL` | 百炼兼容端点 | 旧版单 Key 配置的兼容 Base URL |
| `BAILIAN_EMBEDDING_MODEL` | `text-embedding-v3` | 旧版单 Key 配置的 Embedding 模型 |
| `BAILIAN_EMBEDDING_DIMENSION` | `1024` | 必须与 Milvus Schema 一致 |
| `BAILIAN_LLM_MODEL` | `qwen-plus` | 旧版单 Key 配置的问答模型 |
| `BAILIAN_EMBEDDING_TIMEOUT_SECONDS` / `BAILIAN_LLM_TIMEOUT_SECONDS` | `30` / `60` | 百炼单次请求超时；替代 openai SDK 的 600 秒默认值 |
| `BAILIAN_LLM_MAX_TOKENS` | `2048` | 问答单次回答输出 token 硬上限；命中上限时返回被截断并记录 WARNING |
| `BAILIAN_MAX_RETRIES` | `2` | openai SDK 自动重试次数（默认与 SDK 自身默认值一致，不配置即行为不变）；单请求最坏耗时约 `timeout × (max_retries + 1)`，调小失败更快但减弱 429 限流吸收 |
| `EMBEDDING_BATCH_SIZE` | `10` | 配置校验上限也是 10 |
| `UPLOAD_DIR` | `uploads` | 上传文件存储目录 |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `700` / `100` | 字符级递归切块参数 |
| `MAX_PAGE_CONTENT_BYTES` | `2097152` | 上传文件上限，2 MiB |
| `MAX_DOCUMENTS_PER_WORKSPACE` | `188` | 单个工作空间文档数上限（计数含全部状态）：达到上限后新的剪藏/上传被拒（429，同 URL 重新剪藏不占新额度）；全库检索仅取最新 N 篇 SUCCESS 文档参与（被排除的旧文档记 WARNING），同时给 Milvus 过滤表达式长度封顶 |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` | `localhost` / `6379` / `0` | 异步队列 Redis |
| `INGEST_QUEUE_NAME` | `web-rag:ingest` | 待处理任务队列名 |
| `INGEST_PAYLOAD_TTL_SECONDS` | `604800` | 新网页正文在 MySQL/Redis 中的初始保留期限；重试与恢复不续期 |
| `INGEST_MAX_RETRIES` | `3` | 失败任务最大尝试次数 |

`.env.example` 与 `Settings` 字段一一对应；`CORS_ORIGINS` 为唯一预留项，后端目前尚未注册 CORS 中间件消费它。

## 测试

在已安装 `requirements-dev.txt` 的 Python 3.11 环境与 Node.js 24 下，可用统一入口运行全部离线检查：

```powershell
.venv\Scripts\python.exe scripts/check.py all
```

也可选择 `backend`、`extension`、`datasets` 中一项或多项。扩展检查包含脚本语法检查，并自动发现 `extension/tests/*.test.js`；任一步失败都会返回非零退出码。

`.github/workflows/ci.yml` 在 push、PR 和手动触发时，在 Linux/Windows 上运行后端及扩展检查；Python 使用 3.11、Node 使用 24，Python 依赖按两份 requirements 缓存。工作流只读仓库，不需要 Secrets，不启动 MySQL/Redis/Milvus，不执行收费模型请求。评测步骤允许公开数据中的运行时 ID 占位符，仅校验 150 条数据格式，不代表检索质量评测。真实服务故障恢复与浏览器端到端验证仍需单独执行。

当前已接入的主测试集：

```powershell
.venv\Scripts\python.exe -m pytest -q
```

测试默认不加载个人 `.env`，数据库单元测试使用 SQLite，模型和 Milvus 使用替身。运行 `pytest` 获取当前结果；新增 `backend/tests/test_ingest_reliability.py` 覆盖越权写入、中断恢复、状态竞争、删除补偿及问答事件循环响应。`backend/tests/test_error_contract.py` 用一张 39 行的表逐条实跑「异常类 → 状态码 → 响应体形状（`type`/`code`）」，并双向守门：新增异常处理器必须同时登记该表与 `ARCHITECTURE.md` 的状态码表。

`requirements-dev.txt` 包含运行依赖和 pytest；仅部署运行环境时可安装 `backend/requirements.txt`。

扩展侧的纯 JavaScript 回归测试无需启动后端：

```powershell
node extension/tests/extractor.test.js
node extension/tests/url-utils.test.js
node extension/tests/session-store.test.js
node extension/tests/api-client.test.js
node extension/tests/clip-detect.test.js
node extension/tests/chat-request.test.js
node extension/tests/library-retry.test.js
node extension/tests/sidepanel-jobs.test.js
node extension/tests/page-capture.test.js
```

真实 Chrome 扩展提取回归另行运行，不要求启动后端、不使用 Workspace 凭证：

```powershell
.venv/Scripts/python.exe -m pip install playwright==1.64.1
.venv/Scripts/python.exe scripts/check_capture_browser.py
.venv/Scripts/python.exe scripts/check_scroll_capture_browser.py
```

需要已安装支持扩展调试接口的 Chrome；报告及截图默认存放于被 Git 忽略的 `.venv/runtime/capture-browser/`。该检查通过扩展页面驱动真实 content script，覆盖跨域 iframe、延迟渲染与区域交互；不等于原生 Side Panel 容器及工具栏入口验收。

按需实测公开网页样本（不登录、不处理验证码、不调用后端，结果受网站及网络环境影响）：

```powershell
.venv/Scripts/python.exe scripts/audit_capture_sites.py
```

样本定义在 `extension/tests/site-samples.json`，默认报告和网页快照写入忽略目录 `.venv/runtime/site-audit/`。线上检查不影响 CI；CI 使用 11 份原创 DOM 模式 fixture 重现已修复的缺陷。

可用 `scripts/audit_capture_sites.py --auto-scroll --only github-readme vue-guide` 单独验证开启自动加载后的公开样本。滚动采集原理、停止条件和本机测试见 [自动加载验收](SCROLL_CAPTURE.md)。

## Retrieval 评测

`evaluation/` 包含：

- 70 条主 RAG 样本
- 10 条负例/拒答样本
- 70 条 Workspace 隔离样本
- 两个 Workspace、共 40 篇源文档
- 数据校验、运行时 ID 对齐、基线执行和 Markdown 报告渲染工具

当前公开数据的 chunk 标注仍有部分需要人工重标，因此应把现有结果视为文档级临时基线，不应宣称为完整 chunk 级基线。具体流程见 [evaluation/README.md](evaluation/README.md)。

## 常见问题

### 保存模型配置时报 `APP_MASTER_KEY ... got 0 bytes`

在项目根目录的 `.env` 中设置 `APP_MASTER_KEY`，值必须是 UTF-8 编码后恰好 32 字节的字符串，然后重启 API 和 Worker：

```powershell
python -c "import secrets; print(secrets.token_hex(16))"
```

### 剪藏任务一直排队

确认 `.env` 中的 `REDIS_PORT=16379`，并且第二个终端中的 Worker 正在运行。只有 API 没有 Worker 时，异步任务不会被消费。

### Embedding 或 LLM 显示 `Connection error`

先确认所选服务商、模型和 Key 匹配，再检查系统代理是否允许 Python 访问对应 HTTPS API。配置页会分别验证 Embedding 与 LLM，因此可以判断是哪一侧失败。

### 为什么不能直接更换 Embedding 模型

不同 Embedding 模型产生的向量空间通常不可比较。已有文档时系统会阻止切换；请先删除旧文档，保存新配置后重新剪藏。只更换相同配置的 Key 或更换 LLM 不受影响。

## 已知限制

- 扩展的网页剪藏和文件上传已使用 Redis Worker 异步入库；旧 `/clips` 与 `/documents/upload` 兼容接口仍是同步。
- Workspace 文档数配额（`MAX_DOCUMENTS_PER_WORKSPACE`）包含所有状态。剪藏与上传先做只读预检；所有新建入口（包括 `POST /documents`）在 Repository 持有 Workspace 行锁的创建事务内再次计数，超限返回 429。预检不预留名额，并发任务最终以创建事务的判断为准；同 Workspace 的已有规范化 URL 可复用，满额也允许重新剪藏。上传在原子检查失败时清理新文件。
- 知识库重试从服务端查询文档任务，支持网页与上传、本地记录丢失及关闭原 Tab 的情况；进行中任务直接恢复状态查询。历史同步文档或已丢失正文的任务仍需重新剪藏/上传，且任务重试次数上限保持生效。
- 当前 Worker 是单进程模式；启动时恢复 Redis processing 列表，并在任务边界定期补投 MySQL 中可恢复的排队任务。
- 仅解析 UTF-8 文本和 Markdown；PDF、DOCX、OCR 尚未接入。
- 扩展提供正文/整页/选区/区域四种方式，使用 DOM 候选评分与本地 Readability 补充；部分开放 Shadow DOM、插槽与有权限的可见 iframe 可读取，复杂 SPA、虚拟列表、分页和登录墙仍可能提取不完整。
- 会话历史保存在浏览器 `chrome.storage.local`，后端没有会话表。
- Milvus Collection 已存在时初始化器不会自动迁移 Schema；修改向量维度需重建并重新 ingest。
- 文档删除和 Workspace 删除通过有序、可重试的补偿流程实现，不是跨 MySQL/Milvus/文件系统的分布式事务。
- 当前没有限流、审计、生产级 Secret 轮换或公网部署加固。

## 目录

```text
backend/       FastAPI、业务服务、Repository、模型与测试
extension/     Chrome/Edge Manifest V3 扩展
alembic/       MySQL Schema 迁移（0001 → 0012）
evaluation/    Retrieval/隔离评测数据与工具
uploads/       本地运行时上传目录（Git 忽略）
```
