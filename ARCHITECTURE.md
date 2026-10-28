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
- `extractor.js` 提供 article/page/selection/region 四种模式。智能正文对常见容器评分；无有效候选时在清理后的惰性 DOM 副本上尝试本地 Readability，最后降级到 body。候选准备上限 48，Readability 解析上限 30000 节点。
- 0.8.1 在智能正文模式先按真实域名与可见 DOM 验证少量站点范围，并单独补正文标题；找不到规则时仍走通用算法。代码高亮的 comment 节点不套用评论区清理，行号表按代码列恢复原行，保留列表代码空行、结构缩进和更长 Markdown 围栏。图片为主的短文章有诊断；已识别的短 403/WAF 与知乎受限 JSON 中止捕获，不影响讨论这些错误的普通文章。实测方法和限制见 `SITE_AUDIT_2026-10-28.md`。
- 提取器按模式清噪，把标题、列表、引用、代码、表格和图片替代说明序列化为结构化文本。显式整页/选区/区域保留导航等文字；所有模式排除脚本及输入框值。选区在原节点上判断 CSS 隐藏后复制，开放 Shadow DOM 按渲染插槽展开，避免重复读取 light DOM。
- `capture-content.js` 负责最多 3 秒的内容稳定等待、可见 iframe 元数据及可取消的区域选择。区域高亮放在独立组件中，点击不会打开页面链接；不主动滚动、翻页、展开或发起网络请求。
- `page-capture.js` 由 Side Panel 与 Popup 共用，注入本地打包的 Readability/extractor/capture-content/content，按 frameId 定向通信。通过父页面可见框架 URL 筛选可访问子框架（最多探测 64、提取 12，含顶层），并行提取、去重、标注内嵌来源，合并后统一限制为 500000 字符。权限不足可降级到顶层，并显示诊断。
- `content.js` 是消息入口，保留旧 V3 消息并新增可异步响应的 V4 捕获/描述/取消消息；仅顶层上报 SPA URL。Side Panel 显示模式、结构和质量诊断；切换 Tab、来源导航或身份清理时取消旧捕获。结果返回前再次检查顶层 URL，草稿提交仍校验 URL 与 Workspace 绑定。
- `url-utils.js` 为扩展的“已剪藏”检测提供 URL 规范化；最终写入与查重仍以后端 `normalize_web_url()` 为权威。
- `api-client.js` 集中添加 Plugin Header、解析错误和处理 401；计时覆盖响应头及正文，上传同样使用 AbortController。问答/检索/模型验证的预算从公开服务商目录读取，普通请求 30 秒、上传 120 秒；不自动重试，客户端超时不保证取消服务端处理。晚到的旧凭证 401 不会清除新的身份。
- `session-store.js` 把会话、当前会话和 Tab 上下文存入 `chrome.storage.local`。
- `sidepanel.js` 承担 Workspace 初始化、剪藏、知识库管理、问答和设置交互。问答固定保存到发起时的会话和页面快照，只有该会话仍在显示时才更新聊天区；身份清理后旧结果失效。
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

Embedding/LLM Client 共用有界缓存组件（各自默认最多 32 个缓存项）。调用持有租约，LRU 淘汰和 shutdown 先停止复用，待活动调用释放后关闭连接；关闭异常不覆盖原业务错误。API lifespan 和 Worker 退出时清理客户端。

## 4. 身份、凭证与隔离

### 4.1 Workspace 身份

注册 `POST /plugins/register` 时，服务端生成公开的 `plugin_id` 和只返回一次的 `plugin_secret`。数据库只保存 Secret 的 SHA-256 哈希。

除注册和模型服务商目录外，业务 API 都通过 `get_current_plugin()` 读取 `X-Plugin-ID` 与 `X-Plugin-Secret`，再由 `PluginService.authenticate()` 校验。禁用的 Workspace 返回 403；无效凭证返回 401。`/ingest/page` 也必须通过 DocumentIngestService 校验目标文档归属，不允许绕过文档生命周期直接写向量。

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

`ingest_jobs` 持久化 `QUEUED → RUNNING → SUCCEEDED | FAILED` 状态机，包含 `stage`、`progress`、`attempt_count`、可选 `document_id`、错误摘要和开始/完成时间。`0011` 增加 JSON payload，任务与投递内容在同一事务中提交；`0012` 增加网页正文的 `payload_expires_at` 和索引，在线迁移兼容此前本地 `0011` 已含过期字段的环境。成功时清除 payload；失败正文保留至原始期限，重试不续期。payload 不含模型凭证。Workspace 删除时通过外键级联清理任务；Document 删除时任务历史保留但 `document_id` 置空。普通任务轮询不加载正文。

API 和 Worker 都以 Redis Lua 脚本检查 pending/processing 列表并幂等投递，已有 Redis 正文不刷新 TTL。Redis 不可用时已提交任务保持 QUEUED 并返回 202；Worker 在任务边界扫描 MySQL，每批最多补投 20 个、清理 20 个过期正文，空闲等待默认约 5 秒，执行中不保证固定扫描周期。执行优先读取 MySQL payload，旧任务兼容读取 Redis；无法重建正文的旧网页任务明确失败，不阻塞后续扫描。客户端成功提示不因浏览器本地存储写入失败而丢失；状态 GET 遇暂时网络故障最多重试 5 次，不自动重提业务 POST。

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

两个上传路由均为同步 FastAPI 处理器，在线程池执行有界文件读取及业务操作；已知或未知长度均最多读取配置上限加 1 字节，超限在业务落盘前返回 413。上传异步任务的准备和任务创建在同一线程连续执行，避免请求取消留下已准备但未投递的文档；兼容异步 Service 在该线程自己的事件循环中运行。这不限制进入路由之前 multipart 解析器的缓存或临时文件。

### 6.2 网页剪藏

```text
Side Panel → page-capture.js 按框架注入 Readability + extractor.js + capture-content.js + content.js
  → 候选正文评分 → DOM 副本清噪 → 结构化文本序列化
  → 预览/编辑 + 提取诊断 → 用户确认
  → POST /clips/async → MySQL Job(QUEUED) + Redis payload/queue → 202
  → Worker: processing list → Job(RUNNING)
  → 规范化 URL（移除页内锚点 / 跟踪参数 / 默认端口，保留哈希路由 `#/...`）
  → 按 (plugin_id, normalized_url) 查询既有网页
  → 新建或复用 Document
  → Chunker → Embedding → Milvus
  → 更新 title/url/source_type/chunk_count/status
  → Job(SUCCEEDED, document_id) → ack + 删除 payload
```

网页文档固定使用 `filename=webclip.txt`，不写本地文件。后端会在查重前移除 URL 页内锚点、默认端口和 `utm_*` / `fbclid` / `gclid` 等跟踪参数；唯一的例外是路由型 fragment（`#/...`，旧写法 `#!/...` 会先归一）——哈希路由站点的不同页面只体现在这里，一并剥掉会把 `#/users/1` 与 `#/users/2` 折叠成同一篇文档，后一次剪藏原位覆盖上一篇的分块与向量。同一 Workspace、同一规范化 URL 的失败文档可原位重试；不同 Workspace 的相同 URL 相互独立。

Worker 使用 Redis 待处理列表和 processing 列表实现确认语义。执行正文解析前，在一个 MySQL 事务内领取文档、校验模型指纹、绑定 `job.document_id` 并设置 `stage=PROCESSING_DOCUMENT`。恢复同时枚举 MySQL RUNNING 和 Redis processing，覆盖 Redis 队列丢失的情况。若文档已为 SUCCESS，补记任务成功并清除正文，不重复向量化；否则在同一事务释放本任务占用的 PROCESSING 文档、重排或按次数上限记失败，数据库修复成功后才搬运 Redis 队列。失败任务只释放自己领取的文档，不影响竞争中的其他操作。成功入库后的任务提交、确认失败不会把文档降级为 FAILED。任务写入终态后才确认队列。Worker 执行时才解密当前 Key。

恢复策略仍要求只运行一个 Worker。旧版本未记录文档绑定的网页任务不具备这一恢复保证，更新代码前应先完成在途任务。

### 6.3 Milvus re-ingest

`IngestService` 使用三步收敛算法：查询该 `page_id` 的旧 chunk IDs；以确定性主键 upsert 新 chunks；删除 `old_ids - new_ids` 中的陈旧 chunks。相同输入重复执行可收敛到相同结果，文档变短时也不会遗留尾部 chunk。

## 7. 检索与问答链路

### 7.1 Search

```text
POST /rag/search
  → 解密当前 Workspace API Key
  → 确定检索范围与文档归属/状态；空库直接返回
  → query embedding
  → 构造 Milvus expr
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
  → 按来源逐块填充最多 4000 字符 Context（完整来源头 + 正文片段）
  → System Prompt 约束回答范围、引用编号和资料中的指令
  → 当前 Workspace 配置的 OpenAI-compatible LLM
  → answer + sources
```

没有检索结果、正文为空或预算不足以容纳正文时不调用 LLM，直接返回固定提示和空 Sources。Sources 只包含实际进入 Context 的正文来源，编号与 Context 一致；重复 chunk ID 不重复占用预算。标题和 URL 在提示词内压缩并限制长度，API 返回的来源元数据保持原样。最后一个正文片段可以截短，来源头不会被截断。

回答仅使用本次 retrieval 结果；服务端不读取扩展里的历史会话。空库、越权及非 SUCCESS 文档在调用 Embedding 前就返回或报错。检索和回答的常规日志不记录问题原文。

当前不启用固定相似度拒答阈值。评测工具中的阈值分析是探索性结果，需在真实数据和所选 Embedding 模型上验证；错误前提问题可能需要检索证据来反驳，不能一律按无答案过滤。提示词约束也不代表已经验证了真实模型的拒答或抗指令干扰能力。

## 8. 删除与一致性

`POST /documents` 仅创建虚拟元数据，客户端不得提交非空 `file_path`。上传路径仍由服务端生成；删除虚拟元数据跳过物理文件删除。对于历史记录，删除前若发现同一路径被其他文档引用，会保留文件并拒绝删除，需要先核对归属、清理错误的元数据引用。

Embedding 配置保存和 Document 创建使用同一 Workspace 行锁。配置保存时在锁内重新检查文档；解密后的模型快照在创建/领取文档和执行查询前校验指纹，拒绝因并发模型切换而过期的凭证。旧百炼明文密钥解密后也包装为带模型端点的快照。

文档配额同样在持有该锁的创建事务中检查，覆盖元数据、网页和上传的全部新建入口，按所有状态计数；DI 将同一 Settings 上限注入 Repository 与预检服务。异步剪藏准入按规范化 URL 识别可复用文档，满额仍可重新剪藏。预检不预留名额；上传在落盘后的原子检查失败时补偿删除新文件。

`GET /documents/{document_id}/job` 同时在 SQL 中校验 Job 与 Document 归属，优先返回排队/执行中任务，否则按创建时间、更新时间、ID 倒序选择最近记录。扩展据此重试或恢复轮询，浏览器映射只用于关闭侧栏后的轮询提示；无任务时按网页/上传来源提示重新提交。响应不暴露 payload。

### 8.1 文档删除

删除和重新入库枚举旧 chunk ID 时显式采用 Milvus Strong 一致性，防止 API 客户端因默认 Bounded 读取漏掉 Worker 刚写入的向量。按精确 ID 删除策略保持不变；本机跨进程立即写入/删除验收见 `LIVE_ACCEPTANCE.md`。

删除流程按 Milvus chunks → 本地文件 → MySQL Document 的顺序执行。接口对不存在的文档幂等返回 204；网页文档跳过文件删除。入库和删除用带归属、预期状态及活动任务条件的原子 UPDATE 领取文档，避免先检查再更新的竞争。异步任务尚未提交终态时，其文档也不可被其他操作领取。

这不是分布式事务。若中途失败，文档保持 `DELETING`，避免部分向量已删除却恢复 `SUCCESS`；Side Panel 提供“继续删除”。保留的 MySQL 权威记录使重试可以继续收敛；错误由 API 映射为可观察的 5xx。

删除服务通过线程执行上述同步 I/O，避免阻塞 API 事件循环。Workspace 删除同样使用线程入口；最终删除 Workspace 行前持锁再次确认没有子文档，防止并发新建留下跨存储残余。

### 8.2 Workspace 删除

Workspace 删除要求 `confirm=true` 且提交名称与当前名称完全一致。服务按每批 100 条、始终读取第一页的方式删除所有 Document，再删除 Workspace 行，避免 offset 分页漏项。

## 9. API 与异常边界

当前共有 24 个操作：Plugin 9、Document 8、Clip 2、Job 2、Ingest 1、RAG 2。其中 `POST /plugins/register` 与 `GET /plugins/model-providers` 不要求 Plugin Header。

| 状态码 | 典型场景 |
|---:|---|
| 400 | 非法输入、路径穿越、API Key 验证失败、删除确认失败 |
| 401 | Plugin Header 缺失或凭证无效；`plugin_id` 在库中查不到同样返回 401，不泄露「该 ID 是否存在」的差异 |
| 403 | Workspace 被禁用 |
| 404 | 文档不存在或不属于当前 Workspace |
| 409 | API Key 未配置、文档状态冲突、名称冲突 |
| 413 | 上传超过 2 MiB |
| 415 | 上传类型不支持 |
| 422 | Pydantic/FastAPI 契约校验失败 |
| 429 | Workspace 文档数达 `MAX_DOCUMENTS_PER_WORKSPACE`，拒绝新剪藏/上传 |
| 500 | 切分异常、AES 解密与安全配置异常、文件读写或解析 IO 失败（非输入非法所致） |
| 502 | Embedding 或 LLM 上游调用异常 |
| 503 | MySQL/Milvus 操作异常、异步入库队列不可用 |

错误响应体统一为 `{"detail", "type"}`，`type` 是后端异常类名；只有异步入库任务族返回 `{"detail", "code"}`（`INGEST_JOB_NOT_FOUND` / `INGEST_JOB_CONFLICT` / `INGEST_QUEUE_UNAVAILABLE`）。扩展端 `api-client.js` 的 `classifyResponse` 按状态码 + `type` + `detail` 分类成 `ApiRequestError`；`code` 目前只在一处被读到——`isApiKeyNotConfiguredError` 用它把 IngestJob 族的 409 排除掉，UI 尚未按 `code` 分流。整张「异常类 → 状态码 → 响应体形状」映射由 `backend/tests/test_error_contract.py` 逐条实跑钉住，新增或调整 handler 必须同步登记该表和上面的状态码表。

## 10. 测试、可观测性与维护状态

主测试集覆盖 Router、Service、Repository、DTO、安全工具、URL 规范化、异步队列/Worker、Workspace 隔离和评测计算。具体数量以当前 pytest 输出为准。根目录 conftest.py 禁止测试加载个人 `.env`；可靠性回归使用真实 SQLite Repository 与外部服务替身。扩展另有 Node.js 回归测试。

`scripts/check.py all` 是本地与 GitHub Actions 的统一入口；可分别选择 backend/extension/datasets。CI 在 push、PR、手动触发时以 Python 3.11、Node.js 24 运行 Linux/Windows 矩阵，只授予 contents:read，不需要业务 Secrets 或外部服务。扩展脚本先做语法检查，再自动发现全部 `*.test.js`；数据检查只验证公开占位数据格式，不执行真实评测。真实服务故障恢复与浏览器端到端尚未接入 CI。

日志记录操作类型、Document ID 和候选数量等诊断信息；安全代码避免记录 Plugin Secret 与 API Key 明文。项目目前没有统一 metrics/tracing、结构化审计日志或请求 ID 中间件。

当前工作区没有旧 User/Bearer 路由和服务；历史 User 相关数据库迁移仍保留。`deps.py` 只提供 Plugin 身份。

## 11. 已知演进约束

- 网页和文件 ingest 已异步化；当前恢复策略假定只运行一个 Worker 进程。
- RAG 的同步数据库、向量检索、Embedding 和 LLM 调用通过 `asyncio.to_thread` 执行，避免阻塞 API 事件循环；旧同步入库兼容路径仍不适合作为高并发入口。
- 网页提取基于当前已加载的 DOM；支持部分开放 Shadow DOM/插槽和有权限访问的可见 HTTP(S) iframe。关闭的组件、重定向后无法匹配的框架、分页、虚拟列表、登录墙及超出等待窗口的 SPA 仍需专项适配。PDF/图片 OCR/音视频不在本阶段范围。
- 向量维度或 Collection Schema 改动不能仅改环境变量，必须重建 Collection 并全量重新 ingest。
- 若启用公网访问，需要补齐 CORS、TLS、限流、Secret 轮换、审计和更严格的部署配置。
- 新解析器应实现 `DocumentParser` Protocol，新存储或数据库适配应实现对应 Protocol。
- 根目录 `ARCHITECTURE.md` 是当前架构单一入口；历史阶段文档不作为运行规范。
