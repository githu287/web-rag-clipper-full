# Web RAG Clipper 架构

本文描述当前代码实际采用的架构。历史阶段文档只用于追溯设计过程；当描述冲突时，以本文件、根目录 `README.md`、最新 Alembic 迁移和代码为准。

## 1. 架构目标与边界

系统把浏览器中的网页或本地文本文件转换为可检索知识，并支持两种问答范围：当前文档和当前 Plugin Workspace 的全部知识库。

```text
┌─────────────────────────────────────────────────────────────┐
│ Browser Extension (MV3)                                     │
│ Side Panel / Popup / Content Script / chrome.storage.local  │
└───────────────────────────┬─────────────────────────────────┘
                            │ HTTP
                            │ X-Plugin-ID + X-Plugin-Secret
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ FastAPI                                                     │
│ Router → Service → Repository Protocol → Infrastructure     │
└──────────────┬─────────────────┬─────────────────┬───────────┘
               │                 │                 │
               ▼                 ▼                 ▼
        MySQL 8.0          Milvus 2.4.4      Model APIs
  identity/metadata/state   vectors/chunks   embedding/chat
               │
               ▼
        LocalFileStorage
```

系统是单后端、多 Workspace 的本地优先实现。Workspace 是当前唯一的租户与认证边界；扩展不直接访问数据库或模型服务。

## 2. 分层与依赖方向

| 层 | 目录 | 职责 |
|---|---|---|
| 接口层 | `backend/api/routers` | HTTP 契约、请求校验、依赖注入、响应组装 |
| 业务层 | `backend/services` | 生命周期、跨存储编排、Workspace 隔离、RAG 流程 |
| 抽象/数据层 | `backend/repositories/*/protocol.py`、`backend/models` | Repository Protocol、ORM、DTO、API Schema |
| 基础设施层 | `backend/repositories/*/impl.py`、`clients`、`storage` | MySQL、Milvus、OpenAI 兼容模型服务、本地文件系统适配 |
| 装配层 | `backend/core/di.py` | 根据 Settings 创建并连接依赖 |
| 应用入口 | `backend/main.py` | Router、异常处理器、lifespan、模块级 app |

依赖方向保持为 API → Service → Protocol。Service 不依赖 FastAPI，Router 不直接写 SQL、调用 pymilvus 或构造 OpenAI Client。

## 3. 运行时组件

### 3.1 浏览器扩展

- `background.js` 管理 Side Panel 行为和 Tab URL 变化通知。
- `extractor.js` 对 `article`、`main`、`role=main` 和常见正文容器计算文本长度、链接密度与结构评分；无有效候选时才降级到 `body`。
- 提取器在 DOM 副本中移除脚本、导航、页脚、侧栏、推荐、分享、广告等噪声，并把标题、列表、引用、代码块、表格和图片替代文本序列化为结构化纯文本。
- `content.js` 是消息入口和 SPA URL 监听层。Side Panel 在提交前展示标题、正文、字符数及提取诊断；用户可编辑、取消或重新提取。预览期间如果 Tab 或 URL 变化，草稿会失效，防止正文与来源错绑。
- `url-utils.js` 为扩展的“已剪藏”检测提供 URL 规范化；最终写入与查重仍以后端 `normalize_web_url()` 为权威。
- `api-client.js` 集中添加 Plugin Header、解析错误和处理 401。
- `session-store.js` 把会话、当前会话和 Tab 上下文存入 `chrome.storage.local`。
- `sidepanel.js` 承担 Workspace 初始化、剪藏、知识库管理、问答和设置交互。
- `popup.js` 提供轻量入口，并可引导用户打开 Side Panel。

Plugin Secret 会保存在扩展本地存储以便认证，但不会写入聊天 Session、URL 或页面 DOM。会话仅存在浏览器端，服务端不保存对话历史。

### 3.2 FastAPI

`backend.main:create_app()` 注册 ingest、rag、documents、clips、jobs、plugins 六组 Router。模块级 `app` 供 Uvicorn 直接引用。

lifespan 启动阶段调用 `MilvusInitializer.initialize()`：连接 Milvus；Collection 不存在时创建 Schema 和索引；已存在时校验/复用；最后加载 Collection。它不会自动删除并重建已有 Collection。

已知领域异常由 `main.py` 统一映射为 HTTP 响应，未知异常交给 FastAPI 默认 500。

### 3.3 基础设施

- MySQL：权威保存 Workspace、Document 元数据、归属和生命周期状态。
- Milvus：保存 chunk 文本及向量，用于近似最近邻检索。
- MinIO + etcd：Milvus standalone 的对象存储与元数据依赖。
- Redis：保存待处理队列、processing 确认列表与限时 payload；不作为任务状态权威库。
- 本地文件系统：仅上传文件落入 `uploads/`；网页剪藏不创建物理文件。
- 模型服务：Embedding 和 Chat Completion 均通过 OpenAI 兼容接口访问。Workspace 可分别配置两个服务；内置百炼、OpenAI、Gemini、DeepSeek、硅基流动和 OpenRouter 预设，也支持自定义 HTTPS 兼容端点。

## 4. 身份、凭证与隔离

### 4.1 Workspace 身份

注册 `POST /plugins/register` 时，服务端生成公开的 `plugin_id` 和只返回一次的 `plugin_secret`。数据库只保存 Secret 的 SHA-256 哈希。

除注册外，所有业务 API 都通过 `get_current_plugin()` 读取 `X-Plugin-ID` 与 `X-Plugin-Secret`，再由 `PluginService.authenticate()` 校验。禁用的 Workspace 返回 403；无效凭证返回 401。

### 4.2 模型服务配置

Workspace 的模型 API Key 不参与身份识别。Embedding 与 LLM 各自保存 `provider`、`base_url`、`model` 和 `api_key`，可以来自不同服务商。更新配置时：

1. `model_provider.py` 将服务商预设转换为两个端点；预设 Base URL 不接受客户端覆盖，自定义端点只接受安全的 HTTPS URL。
2. 使用提交的 Embedding Key 发起最小向量请求，并校验返回维度为 1024。
3. 使用提交的 LLM Key 发起最小 Chat Completion 请求。
4. 使用 `APP_MASTER_KEY` 对版本化 JSON 配置做 AES-256-GCM 加密，将 ciphertext 与独立 nonce 保存到 `plugin_workspaces`。
5. 业务请求中按当前 Workspace 解密；EmbeddingClient 与 LLMClient 各自选择对应的 Key、Base URL 和模型。

旧版单百炼 Key 仍可读取，并在运行时映射成百炼 Embedding + 百炼 LLM 双端点。数据库和 API 响应均不保存或回显明文 Key。

`embedding_config_fingerprint` 记录不含 Key 的向量空间指纹。Workspace 已有文档时，配置服务会拒绝改变 Embedding 服务商、Base URL、模型或维度参数，避免同一 Milvus Collection 中混入不可比较的向量；仅轮换 Key 或修改 LLM 不受影响。清除凭证时保留该指纹。

`APP_MASTER_KEY` 必须是 UTF-8 编码后恰好 32 字节的字符串。

### 4.3 多层隔离

Workspace 隔离不只依赖一个过滤点：

1. API 身份来自双 Header，客户端不能在请求体指定 `plugin_id`。
2. Document Repository 的读取、列表、删除都带 `plugin_id` 条件。
3. 全库检索先从 MySQL 获取当前 Workspace 的 `SUCCESS` 文档 ID。
4. Milvus 搜索表达式限制 `page_id in [...]`；指定文档模式使用 `page_id == document_id`。
5. 候选返回后再次按 MySQL 归属和状态过滤，孤儿 chunk 也会被移除。
6. 跨 Workspace 的具体文档访问统一返回 404，避免泄露资源是否存在。

## 5. 核心数据模型

### 5.1 `plugin_workspaces`

| 字段 | 作用 |
|---|---|
| `id` | 内部自增主键 |
| `plugin_id` | 对外 Workspace 标识，唯一 |
| `plugin_name` / `plugin_name_norm` | 展示名与唯一归一化名 |
| `plugin_secret_hash` | Secret 的 SHA-256 哈希 |
| `api_key_ciphertext` / `api_key_nonce` | AES-GCM 加密的版本化模型配置；可为空，兼容旧百炼 Key 密文 |
| `embedding_config_fingerprint` | 不含 Key 的 Embedding 空间 SHA-256 指纹；可为空 |
| `status` | `ACTIVE` / `DISABLED` / `DELETING` |
| `created_at` / `updated_at` | 时间戳 |

### 5.2 `documents`

| 字段组 | 字段 |
|---|---|
| 标识与归属 | `id`、`plugin_id` |
| 文件 | `filename`、`file_path`、`file_size`、`mime_type` |
| 来源 | `title`、`url`、`source_type` (`upload` / `webpage`) |
| 生命周期 | `status`、`chunk_count`、`error_message` |
| 时间 | `created_at`、`updated_at` |

数据库还保留旧迁移产生的 `user_id NOT NULL DEFAULT 0` 列以支持回滚；当前 ORM 和业务归属不使用该列。

### 5.3 Milvus `page_chunks`

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | `VARCHAR(64)` | 主键，`{page_id}_{chunk_index}` |
| `page_id` | `INT64` | 等于 `documents.id` |
| `chunk_index` | `INT64` | 从 0 开始 |
| `chunk_text` | `VARCHAR(4096)` | DTO 按 UTF-8 字节校验 |
| `embedding` | `FLOAT_VECTOR(1024)` | 与所有可选 Embedding 服务的输出维度一致 |

向量索引为 HNSW，距离度量为 COSINE，搜索参数 `ef=128`。代码中的 `distance` 实际承载相似度，值越大越相似。

### 5.4 `ingest_jobs`

`ingest_jobs` 持久化 `QUEUED → RUNNING → SUCCEEDED | FAILED` 状态机，包含 `stage`、`progress`、`attempt_count`、可选 `document_id`、错误摘要和开始/完成时间。Workspace 删除时通过外键级联清理任务；Document 删除时任务历史保留但 `document_id` 置空。

## 6. 写入链路

### 6.1 文件上传

```text
Side Panel 选择文件 → POST /documents/upload/async
  → 校验名称、扩展名、大小
  → LocalFileStorage.save
  → INSERT Document(PENDING, source_type=upload)
  → MySQL Job(QUEUED, type=FILE_UPLOAD, document_id) + Redis 引用 → 202
  → Worker: processing list → Job(RUNNING)
  → status=PROCESSING
  → TextDocumentParser
  → RecursiveCharacterChunker
  → EmbeddingClient（每批最多 10 条）
  → Milvus re-ingest
  → status=SUCCESS + chunk_count
  → Job(SUCCEEDED) → ack + 删除 payload
```

失败后 Document 和 Job 都进入 `FAILED` 并记录错误摘要。文件在 Document 创建成功后的处理失败场景中会保留以支持重试；若 Document 创建本身失败，会清理刚落盘的孤儿文件。Redis payload 只包含 Workspace 和 Document 引用，不包含文件字节或 API Key。旧 `POST /documents/upload` 保留为同步兼容接口。

### 6.2 网页剪藏

```text
Side Panel 注入 extractor.js + content.js
  → 候选正文评分 → DOM 副本清噪 → 结构化文本序列化
  → 预览/编辑 + 提取诊断 → 用户确认
  → POST /clips/async → MySQL Job(QUEUED) + Redis payload/queue → 202
  → Worker: processing list → Job(RUNNING)
  → 规范化 URL（移除 fragment / 跟踪参数 / 默认端口）
  → 按 (plugin_id, normalized_url) 查询既有网页
  → 新建或复用 Document
  → Chunker → Embedding → Milvus
  → 更新 title/url/source_type/chunk_count/status
  → Job(SUCCEEDED, document_id) → ack + 删除 payload
```

网页文档固定使用 `filename=webclip.txt`，不写本地文件。后端会在查重前移除 URL fragment、默认端口和 `utm_*` / `fbclid` / `gclid` 等跟踪参数。同一 Workspace、同一规范化 URL 的失败文档可原位重试；不同 Workspace 的相同 URL 相互独立。

Worker 使用 Redis 待处理列表和 processing 列表实现确认语义。任务写入 MySQL 终态后才从 processing 移除；Worker 重启时会将未确认项恢复到队列并把中断的 `RUNNING` 任务置回 `QUEUED`。Redis payload 不包含用户 API Key，Worker 执行时才从 Workspace 解密当前 Key。

### 6.3 Milvus re-ingest

`IngestService` 使用三步收敛算法：查询该 `page_id` 的旧 chunk IDs；以确定性主键 upsert 新 chunks；删除 `old_ids - new_ids` 中的陈旧 chunks。相同输入重复执行可收敛到相同结果，文档变短时也不会遗留尾部 chunk。

## 7. 检索与问答链路

### 7.1 Search

```text
POST /rag/search
  → 解密当前 Workspace API Key
  → query embedding
  → 确定检索范围与 Milvus expr
  → Milvus HNSW/COSINE 候选
  → MySQL 批量反查 Document
  → Workspace + SUCCESS + orphan 后过滤
  → top-K + 文档来源元数据
```

全知识库模式候选数为 `max(limit, 10)`；当前文档模式会扩大候选池后再限定到目标文档。API 的 `limit` 范围是 1–20。

### 7.2 Ask

```text
POST /rag/ask
  → 可选 document_id 的归属与 SUCCESS 前置校验
  → RagService.search(top_k=5)
  → 最多 4000 字符 Context
  → 固定六条约束 System Prompt
  → 当前 Workspace 配置的 OpenAI-compatible LLM
  → answer + sources
```

没有检索结果时不调用 LLM，直接返回固定提示和空 Sources。回答仅使用本次 retrieval 结果；服务端不读取扩展里的历史会话。

## 8. 删除与一致性

### 8.1 文档删除

删除流程按 Milvus chunks → 本地文件 → MySQL Document 的顺序执行。接口对不存在的文档幂等返回 204；网页文档跳过文件删除。`DELETING` 状态用于阻止 ingest 与 delete 并发进入。

这不是分布式事务。若中途失败，保留的 MySQL 权威记录使重试可以继续收敛；错误由 API 映射为可观察的 5xx。

### 8.2 Workspace 删除

Workspace 删除要求 `confirm=true` 且提交名称与当前名称完全一致。服务按每批 100 条、始终读取第一页的方式删除所有 Document，再删除 Workspace 行，避免 offset 分页漏项。

## 9. API 与异常边界

当前共有 23 个操作：Plugin 9、Document 7、Clip 2、Job 2、Ingest 1、RAG 2。其中 `POST /plugins/register` 与 `GET /plugins/model-providers` 不要求 Plugin Header。

| 状态码 | 典型场景 |
|---:|---|
| 400 | 非法输入、路径穿越、API Key 验证失败、删除确认失败 |
| 401 | Plugin Header 缺失或凭证无效 |
| 403 | Workspace 被禁用 |
| 404 | 文档不存在或不属于当前 Workspace |
| 409 | API Key 未配置、文档状态冲突、名称冲突 |
| 413 | 上传超过 2 MiB |
| 415 | 上传类型不支持 |
| 422 | Pydantic/FastAPI 契约校验失败 |
| 502 | Embedding 或 LLM 上游调用异常 |
| 503 | MySQL/Milvus 操作异常 |

## 10. 测试、可观测性与维护状态

主测试集覆盖 Router、Service、Repository、DTO、安全工具、URL 规范化、异步队列/Worker、Workspace 隔离、评测计算和报告渲染。当前活动集实测为 563 passed，并包含 37 个 subtests。扩展另有 Node.js 回归测试，覆盖正文候选评分、噪声识别、结构化文本规范化、URL 规范化和任务持久化。

日志记录操作类型、Document ID 和候选数量等诊断信息；安全代码避免记录 Plugin Secret 与 API Key 明文。项目目前没有统一 metrics/tracing、结构化审计日志或请求 ID 中间件。

工作区中存在未接入主应用的旧 User/Bearer 草稿。`main.py` 不注册 `auth.py` 与 `users.py`，`deps.py` 也只提供 Plugin 身份；这些文件不属于当前运行架构。

## 11. 已知演进约束

- 网页和文件 ingest 已异步化；当前恢复策略假定只运行一个 Worker 进程。
- 网页正文提取基于浏览器可见 DOM；Shadow DOM、跨域 iframe、分页内容、登录墙及尚未渲染完成的 SPA 需要后续专项适配。
- 向量维度或 Collection Schema 改动不能仅改环境变量，必须重建 Collection 并全量重新 ingest。
- 若启用公网访问，需要补齐 CORS、TLS、限流、Secret 轮换、审计和更严格的部署配置。
- 新解析器应实现 `DocumentParser` Protocol，新存储或数据库适配应实现对应 Protocol。
- 根目录 `ARCHITECTURE.md` 是当前架构单一入口；历史阶段文档不作为运行规范。
