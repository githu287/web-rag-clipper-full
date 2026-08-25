# web-rag-clipper-full — Phase 0 架构设计

> **阶段约束**：本阶段不生成任何业务代码、不创建 FastAPI/插件/Repository 实现文件。后续每一阶段需确认后再进入。
>
> **Phase 0.1 修正**：统一异步 ingest 机制、content_hash 权威来源、API Contract、MySQL/Milvus 一致性规则及第一版 reconcile 能力边界。

---

## 1. 项目目标

构建一个**个人网页知识库 RAG Assistant**，用户通过 Chrome 插件主动抓取当前网页内容，后端完成存储、向量化与检索，并基于阿里云百炼提供摘要与问答能力。

### 第一版必须交付的三项能力

| 能力 | 说明 |
|------|------|
| 网页保存 | 用户点击插件 → 提取当前页正文 → 后端持久化；以 `content_hash` 去重 |
| 网页摘要 | 用户**主动触发** → 后端调用百炼 LLM 生成摘要并缓存 |
| 知识库 RAG 问答 | 用户提问 → 向量检索 Milvus chunks → 百炼 LLM 生成带引用的回答 |

### 角色分工

- **Chrome 插件**：内容提取 + 后端 API 调用（不做 AI 推理、不做 content_hash 权威计算）
- **FastAPI 后端**：业务编排、状态管理、content_hash 权威计算、数据一致性
- **MySQL**：业务元数据（页面、文档、处理状态、摘要等）
- **Milvus**：向量 + Chunk 检索数据
- **Redis**：缓存、处理状态、幂等控制
- **阿里云百炼**：远程 Embedding + LLM（OpenAI 兼容 API，本地不部署模型）

### 用户模型（已确认）

单用户 / 个人本地使用，无需完整账号体系，可用固定 API Token 鉴权。

---

## 2. 当前确定的技术栈

```
┌─────────────────────────────────────────────────────────────┐
│  Chrome Extension MV3 (activeTab + scripting)               │
│  职责：内容提取、API 调用                                     │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTPS + API Token
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  FastAPI Backend                                            │
│  API Layer → Service Layer → Repository Layer               │
│  异步 ingest：BackgroundTasks（无持久化）                     │
└───┬──────────────┬──────────────┬───────────────────────────┘
    │              │              │
    ▼              ▼              ▼
  MySQL          Milvus         Redis
  业务元数据      向量+Chunks     缓存/状态/幂等
                           │
                           ▼
              阿里云百炼 OpenAI 兼容 API
              (Embedding + LLM)
```

| 层级 | 技术 | 职责 |
|------|------|------|
| 浏览器端 | Chrome Manifest V3 | 插件框架 |
| 浏览器端 | `activeTab` + `scripting` | 用户主动点击时注入脚本提取内容 |
| 浏览器端 | Background Service Worker | Token 管理、API 路由、消息协调 |
| 后端 | Python + FastAPI | HTTP API、业务编排 |
| 后端 | FastAPI BackgroundTasks | 异步 ingest（第一版唯一方案） |
| 元数据 | MySQL | 页面记录、处理状态、摘要文本、去重索引 |
| 向量检索 | Milvus | Embedding 向量、Chunk 文本与 metadata |
| 缓存/状态 | Redis | 摘要缓存、处理状态、幂等键、短期锁 |
| AI 服务 | 阿里云百炼 OpenAI 兼容 API | `text-embedding-*` + `qwen-*` 等 |
| 配置 | `.env` / 环境变量 | 所有密钥、连接串、模型名 |

**明确不引入：** Celery、Kafka、RabbitMQ 等消息队列；`asyncio.create_task` 作为 ingest 调度方案；本地 LLM；Elasticsearch；MongoDB；GraphQL；content_scripts 全站注入。

---

## 3. API Contract（全项目统一）

后续所有阶段**必须严格**按以下路径实现，不得使用其他变体：

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/pages` | 保存网页（含去重、触发异步 ingest） |
| `POST` | `/api/pages/{page_id}/summary` | 按需生成摘要 |
| `POST` | `/api/pages/{page_id}/retry` | 手动重试 ingest |
| `POST` | `/api/chat` | 知识库 RAG 问答 |

**禁止使用：** `POST /pages`、`POST /chat`、`POST /api/webpage/save`、`POST /api/webpage/summary` 等路径。

**鉴权：** 所有 API 使用 `Authorization: Bearer {API_TOKEN}`。

---

## 4. 当前确定的模块边界

### 4.1 Chrome Extension（`extension/`）

| 模块 | 职责 | 不负责 |
|------|------|--------|
| Popup UI | 保存 / 摘要 / 问答入口、状态展示 | AI 推理、向量计算、content_hash 权威计算 |
| Background Service Worker | Token 管理、API 路由、消息协调 | 页面 DOM 访问 |
| Content Script（按需注入） | 提取 title、url、content（正文文本） | 自动监听所有页面 |
| Config | 后端 Base URL、API Token（storage） | 硬编码密钥 |

**页面限制（明确不支持）：** `chrome://`、Chrome 内置 PDF 预览、Canvas 渲染内容、iframe 跨域不可读内容等特殊页面 → 插件应给出明确错误提示。

### 4.2 Backend FastAPI（`backend/`）

采用 **API → Service → Repository** 三层，禁止把所有逻辑写进 `main.py`。

```
backend/
├── main.py              # 应用入口，仅注册路由/中间件/生命周期
├── api/                 # 路由层：请求校验、响应格式
│   ├── pages.py         # POST /api/pages、/api/pages/{id}/retry
│   ├── summary.py       # POST /api/pages/{id}/summary
│   └── chat.py          # POST /api/chat
├── services/            # 业务层：编排、状态机、一致性策略
│   ├── page_service.py
│   ├── ingest_service.py    # 切分、Embedding、写入 Milvus（BackgroundTasks 调度）
│   ├── summary_service.py
│   └── rag_service.py
├── repositories/        # 数据访问层
│   ├── mysql/           # 页面、状态、摘要
│   ├── milvus/          # 向量、Chunks
│   └── redis/           # 缓存、幂等、处理锁
├── models/              # Pydantic schemas + ORM models
├── core/                # 配置、依赖注入、异常、常量、normalize/content_hash
└── clients/             # 百炼 OpenAI 兼容客户端封装
```

### 4.3 数据职责边界

| 存储 | 存什么 | 不存什么 |
|------|--------|----------|
| **MySQL** | `pages`（url, title, content_hash, raw_text, status, timestamps）、`summaries`、处理日志 | 向量、Chunk 全文检索 |
| **Milvus** | Chunk embedding 向量、chunk_text、page_id、chunk_index | 业务状态机、用户-facing 元数据 |
| **Redis** | 摘要缓存、幂等键（如 `idempotency:save:{hash}`）、处理中锁、RAG 热点缓存 | 持久化主数据 |

### 4.4 处理状态（MySQL 权威源）

```
PROCESSING → SUCCESS
           → FAILED
```

- `PROCESSING`：已接收，BackgroundTasks 正在执行切分 / Embedding / 写 Milvus
- `SUCCESS`：**Milvus 数据已成功写入**，MySQL 元数据就绪（见 §6 一致性规则）
- `FAILED`：ingest 任一步骤失败，记录 `error_message`，可通过 retry 补偿

---

## 5. 当前确定的开发原则

1. **先设计、后编码、分阶段实现** — 每阶段仅修改该阶段文件，需确认后进入下一阶段
2. **零硬编码密钥** — 所有配置通过 `.env` / 环境变量；提供 `.env.example`
3. **不擅自增删技术栈或改架构** — 发现问题先报告，等待确认
4. **不用伪代码冒充完整实现**
5. **不为跑通而删功能**
6. **后端严格分层** — API 不含业务逻辑，Repository 不含编排逻辑
7. **插件最小权限** — 仅 `activeTab` + `scripting`，不用 `<all_urls>` 自动注入
8. **AI 远程化** — Embedding 与 LLM 全部走百炼 API
9. **幂等与去重** — 保存以**后端计算的** `content_hash` 为唯一业务键
10. **一致性优先设计** — MySQL 为状态权威；Milvus 写入失败必须标记 FAILED 并支持 retry

---

## 6. 核心业务流程

### 6.1 content_hash 权威来源

**Chrome 插件职责：** 仅提取 `title`、`url`、`content`（正文文本），提交给后端。

**权威计算在后端：**

```
normalized_content = normalize(content)
content_hash = SHA256(normalized_content)
```

- 客户端**可以**预计算 hash 用于本地提示，但**不得**作为去重权威值
- 后端重新 normalize 并计算 hash，结果作为**唯一权威**
- **去重规则：** 对规范化正文文本（去空白、统一换行）做 SHA-256；**不含 URL**；同一正文内容视为相同内容

### 6.2 网页保存（含去重）

1. 用户点击插件「保存网页」
2. 插件通过 `scripting` 提取 `title` / `url` / `content`
3. `POST /api/pages` 提交到后端（body 含 title、url、content）
4. 后端执行权威 hash 计算：`normalize(content)` → `SHA-256` → `content_hash`
5. 后端检查 Redis 幂等键 / 分布式锁
6. MySQL 按**后端计算的** `content_hash` 查重
   - **已存在且 SUCCESS** → 返回已有 page（200）
   - **新内容或需重试** → INSERT/UPDATE `status=PROCESSING`，返回 202
7. 通过 **FastAPI BackgroundTasks** 调度异步 ingest（见 §6.4）
8. ingest 成功 → MySQL `status=SUCCESS`；失败 → `status=FAILED` + `error_message`

### 6.3 网页摘要（按需触发，已确认）

1. 用户点击「生成摘要」
2. `POST /api/pages/{page_id}/summary`
3. 查 Redis 摘要缓存 → 命中则直接返回
4. 未命中 → 从 MySQL 取 `raw_text`（需 `status=SUCCESS`）
5. 调用百炼 LLM 生成摘要
6. 持久化到 MySQL，写入 Redis 缓存
7. 返回摘要

### 6.4 异步 ingest 流程（BackgroundTasks）

**第一版统一使用 FastAPI BackgroundTasks，不使用 `asyncio.create_task`，不引入 Celery / Kafka / RabbitMQ。**

**重要约束：**

- BackgroundTasks **不具备持久化能力**
- 后端进程重启会导致正在执行的 PROCESSING 任务**中断**
- 必须通过 **PROCESSING 超时检测** + **`POST /api/pages/{page_id}/retry`** 进行补偿

**正常 ingest 流程：**

```
POST /api/pages 接受请求
  → MySQL status = PROCESSING
  → 返回 202
  → BackgroundTasks 调度 ingest_service：
       1. 文本切分（Chunk）
       2. 百炼 batch Embedding
       3. Milvus upsert（page_id + chunk_index）
       4. Milvus 写入成功 → MySQL status = SUCCESS
       5. Milvus 写入失败 → MySQL status = FAILED + error_message
```

**ingest 失败重试（任务内）：** 最多 3 次指数退避；仍失败则标记 FAILED。

**进程中断补偿：**

- PROCESSING 超过配置超时（如 600s）→ 可被检测并标记 FAILED / 待重试
- 用户或运维调用 `POST /api/pages/{page_id}/retry` 重新触发 ingest

### 6.5 知识库 RAG 问答

1. 用户输入问题
2. `POST /api/chat {question}`
3. 百炼生成 query embedding
4. Milvus top-k 相似 chunks 检索（第一版：全库检索）
5. 百炼 LLM RAG（context + question）
6. 返回回答 + 引用来源（page_id / url / title）

### 6.6 MySQL ↔ Milvus 一致性规则

#### 正常成功路径（唯一 SUCCESS 语义）

```
MySQL PROCESSING
  → Chunk 切分
  → Embedding
  → Milvus 写入成功
  → MySQL SUCCESS
```

**因此：`SUCCESS` 表示 Milvus 数据已经成功写入。** 不存在「正常流程下 SUCCESS 但 Milvus 无数据」的情况。

#### 失败路径

| 阶段 | MySQL 状态 | 说明 |
|------|-----------|------|
| Milvus 写入失败 | `FAILED` | 必须保持/更新为 FAILED，记录 `error_message` |
| ingest 任务内重试耗尽 | `FAILED` | 等待 manual retry |
| 后端重启导致任务中断 | `PROCESSING`（悬挂） | 超时检测后标记 FAILED / 待重试 |

#### 异常一致性问题（非正常流程）

| 场景 | 性质 | 处理 |
|------|------|------|
| MySQL SUCCESS 但 Milvus 数据缺失 | **异常** | reconcile / re-ingest 机制处理，不得视为正常 |
| Milvus 写入成功但 MySQL 更新 SUCCESS 失败 | **异常** | 以 page_id 关联；补偿扫描 PROCESSING 超时记录 |
| 重复 ingest 同一 page | 正常重试 | Milvus upsert by `(page_id, chunk_index)` |

### 6.7 第一版 reconcile 能力边界

第一版**不引入** Celery / Kafka 等持久化任务队列，**不设计**依赖消息队列的自动调度系统。

**第一版必须提供：**

- `POST /api/pages/{page_id}/retry` — 手动重试 ingest
- PROCESSING 超时记录可被检测并进入 FAILED / 待重试状态

**第一版可选（后续阶段，不引入新基础设施）：**

- 应用启动时轻量级扫描：将超时 PROCESSING 标记为 FAILED
- 不引入 Celery / Kafka / RabbitMQ

**后续阶段如需自动 reconcile，** 可在不新增基础设施前提下设计轻量级启动扫描等方案；当前 Phase 0 不引入。

---

## 7. 当前明确不做的功能

- 多用户 / 注册登录 / OAuth 账号体系
- 保存时自动生成摘要
- `content_scripts` + `<all_urls>` 自动注入
- 支持 `chrome://`、PDF 预览页、Canvas 渲染页
- 本地部署 LLM / Embedding 模型
- 插件端 AI 推理
- 插件端 content_hash 作为去重权威
- `asyncio.create_task` 作为 ingest 调度
- Celery / Kafka / RabbitMQ 等消息队列
- 依赖消息队列的自动 reconcile 调度系统
- 擅自引入额外搜索引擎（Elasticsearch 等）
- 伪代码占位、main.py 大杂烩
- 硬编码 API Key / 连接串

---

## 8. 架构决策确认（A–H）

| # | 决策项 | 确认方案 |
|---|--------|----------|
| A | `content_hash` 权威来源 | 后端 `normalize(content)` → SHA-256；不含 URL；客户端计算不作权威 |
| B | 异步 ingest 实现 | **仅** FastAPI BackgroundTasks；无持久化；重启需超时检测 + retry 补偿 |
| C | Chunk 策略 | 500–800 字符/块，100 字符 overlap |
| D | Embedding / LLM 模型 | 通过 `.env` 配置（如 `text-embedding-v3` + `qwen-plus`） |
| E | RAG 检索范围 | 第一版全库检索（单用户无隔离） |
| F | 插件 ↔ 后端通信 | HTTPS + `Authorization: Bearer {API_TOKEN}` |
| G | ingest 失败重试 | 任务内最多 3 次指数退避；暴露 `POST /api/pages/{page_id}/retry` |
| H | 项目目录结构 | 单 repo：`extension/` + `backend/` + `docker-compose.yml` |

---

## 9. 已识别架构风险

1. **BackgroundTasks 无持久化** — 后端重启会导致 PROCESSING 任务中断；必须依赖 Redis 锁 + PROCESSING 超时检测 + `POST /api/pages/{page_id}/retry` 补偿；第一版不提供 Celery 级自动恢复
2. **Milvus 与 MySQL 双写非原子** — SUCCESS 仅在 Milvus 写入成功后设置；异常情况下可能出现 SUCCESS/Milvus 不一致，需 reconcile 处理
3. **Milvus 与 pymilvus 版本兼容** — Milvus 服务端版本必须与 pymilvus 客户端版本保持兼容；后续不得随意单独升级其中一个；**Phase 2 开始前需确认最终版本组合**
4. **百炼 API 限流/超时** — Embedding 批处理需分批 + 重试；ingest 耗时会较长
5. **大页面文本** — 需限制单次上传大小；切分前截断或拒绝
6. **CORS** — Extension 从 `chrome-extension://` origin 调用后端，FastAPI 需配置允许来源
7. **特殊页面提取质量** — SPA、懒加载页面可能提取不完整；第一版接受「尽力提取」并记录原文长度

---

## 10. 后续阶段预览

| 阶段 | 内容 |
|------|------|
| Phase 1 | 项目脚手架：目录结构、`.env.example`、`docker-compose`（MySQL/Redis/Milvus）、依赖清单 |
| Phase 2 | 数据模型与 Repository 接口定义（开始前确认 Milvus/pymilvus 版本组合） |
| Phase 3 | 网页保存 + ingest 流水线 |
| Phase 4 | 按需摘要 |
| Phase 5 | RAG 问答 |
| Phase 6 | Chrome 插件 |
| Phase 7 | 一致性补偿与联调 |

---

## Phase 0 / 0.1 交付物

- 本文档（`docs/PHASE0_ARCHITECTURE.md`）
- **无业务代码文件**
