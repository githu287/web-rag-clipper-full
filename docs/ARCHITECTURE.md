# 架构设计文档（当前实现）

> 本文档描述 **当前实现** 的架构事实，与 `docs/` 下 PHASE* 历史设计文档互补。历史文档为阶段演进记录，如与本文冲突，以本文档与代码为准。

## 1. 系统概览

**产品定位**：Web RAG Clipper —— 面向网页内容剪藏与知识库构建的 RAG 系统。产品目标是通过浏览器扩展采集网页内容，送入后端知识库完成解析、切分、向量化和 RAG 检索。

**当前阶段**：已完成 RAG 后端核心链路（FastAPI 后端、Document 生命周期、文件上传、Text Parser、Chunker、Embedding、Milvus、MySQL、RAG Search、Retry/Delete、SUCCESS-only 与 orphan 过滤、RAG 元数据、三方一致性），浏览器扩展端尚未实现；`extension/` 目录目前仅作为未来插件端的结构占位。

**系统边界**：

```
[Browser Extension]（规划 / 未实现）
        │  网页内容采集 / 剪藏入口
        ▼
[FastAPI Backend]（当前已实现）
        │
        ▼
Document → Parser → Chunker → Embedding → Milvus
        │
        ▼
   RAG Search（检索结果 + Document 元数据）
```

**后端核心链路**（当前已实现）：

```
上传文档 → FileStorage → Document → Parser → Chunker → Embedding → Milvus → RAG Search → 检索结果 + Document 元数据
```

**核心设计原则**：

1. **MySQL 为状态权威，Milvus 为向量载体**：Document 生命周期状态只以 `documents` 表为准；Milvus 仅存向量数据，检索后回查 MySQL 过滤并补充元数据。
2. **`document.id == Milvus.page_id` 1:1 映射**：Document 主键直接作为 Milvus 分页 ID，消除双系统 ID 转换。
3. **分层依赖倒置**：API → Service → Repository(Protocol) → 基础设施，全部通过 DI 工厂装配，无直接实例化。
4. **异常分层 + 全局映射**：领域异常在 `core/exceptions.py` 定义，`main.py` 全局 handler 统一转 HTTP，Router/Service 不吞异常。
5. **显式并发互斥**：`DELETING` 状态作为 Delete ↔ Ingest/Retry 的互斥 gate。

## 2. 技术栈与版本锁定

| 层 | 技术 | 版本策略 |
|---|---|---|
| 运行时 | Python | 3.11.9 |
| Web | FastAPI / Uvicorn | `>=0.115,<1.0` |
| 校验 | Pydantic v2 / pydantic-settings | `>=2.8,<3.0` |
| 关系库 | MySQL 8.0（compose `mysql:8.0`） | 宿主 `33066` 暴露 |
| ORM / 迁移 | SQLAlchemy 2.0 / PyMySQL / Alembic | `>=2.0,<3.0`；head=`0002` |
| 向量库 | Milvus v2.4.4 standalone + pymilvus==2.4.15 | **版本锁死**：pymilvus 禁止 2.5.x+ / 3.x 跨 minor |
| Embedding | 百炼 `text-embedding-v3`（OpenAI 兼容） | dim=1024，batch≤10 |
| LLM（预留） | 百炼 `qwen-plus` | 未接入业务 |
| Redis（预留） | redis 7-alpine | compose 部署，业务未使用 |
| 测试 | unittest | 无第三方测试框架 |

Milvus standalone 依赖：etcd v3.5.5（元数据）+ MinIO（存储），均由 `docker-compose.yml` 编排。

## 3. 分层架构

> 上游说明：`Browser Extension`（规划 / 未实现）位于 API 层之上，未来作为网页内容采集入口；当前数据入口为后端文件上传 API。

```
┌────────────────────────────────────────────────────────┐
│ API 层  backend/api/routers/                          │
│   documents.py  ingest.py  rag.py                     │
│   职责：Pydantic 校验、调用 Service、构造响应；不触碰基础设施 │
└──────────────────────┬─────────────────────────────────┘
                       │ Depends(DI 工厂)
┌──────────────────────▼─────────────────────────────────┐
│ Service 层  backend/services/                          │
│   DocumentUploadService   上传 + 全链路编排             │
│   DocumentIngestService   Document 生命周期 + 重试       │
│   DocumentDeleteService   幂等删除                     │
│   IngestService           chunk 入库编排（re-ingest）    │
│   RagService              RAG 检索                     │
└───┬────────┬────────┬────────┬────────┬───────────────┘
    ▼        ▼        ▼        ▼        ▼
Repository  Storage  Parser  Chunker  Client
 MySQL 协议  Local    Text    Recursive  Bailian Embedding
 Milvus 协议
```

**依赖倒置**：`repositories/mysql` 与 `repositories/milvus` 各自定义 `Protocol`（接口）与 `Impl`（实现），Service 只依赖 Protocol。测试中可注入 mock 实现，无需真实 MySQL/Milvus。

## 4. 核心数据模型

### 4.1 MySQL `documents` 表（状态权威，11 字段）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | INT | PK, AUTO_INCREMENT | Document 主键 = Milvus `page_id` |
| `user_id` | INT | NULL, 索引 | 所属用户（无 user 体系，恒 NULL） |
| `filename` | VARCHAR(255) | NOT NULL | 文件名 |
| `file_path` | VARCHAR(512) | NOT NULL | 存储路径 |
| `status` | VARCHAR(32) | NOT NULL, 默认 `PENDING`, 索引 | 生命周期状态 |
| `chunk_count` | INT | NOT NULL, 默认 0 | 已入库 chunk 数 |
| `created_at` | DATETIME | NOT NULL, 默认 now | 创建时间 |
| `updated_at` | DATETIME | NOT NULL, ON UPDATE now | 更新时间 |
| `file_size` | INT | NOT NULL, 默认 0 | 字节数 |
| `mime_type` | VARCHAR(128) | NOT NULL, 默认 `''` | MIME 类型 |
| `error_message` | TEXT | NULL | 失败摘要（截断 2048） |

设计要点：`status` 使用 `String(32)` + 应用层常量（`DocumentStatus`），不用 SQLAlchemy Enum，避免 migration 痛点。

### 4.2 Milvus `page_chunks` Collection（向量载体，5 字段）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | VARCHAR(64) PK | `{page_id}_{chunk_index}`（如 `1_3`） |
| `page_id` | INT64 | = `documents.id` |
| `chunk_index` | INT64 | 0 起序号 |
| `chunk_text` | VARCHAR(4096) | 切块文本 |
| `embedding` | FLOAT_VECTOR(1024) | 百炼向量 |

索引与检索：

- 向量索引：`HNSW`（metric=`COSINE`，M=16，efConstruction=200）
- 标量索引：`page_id` → `INVERTED`
- 检索参数：`ef=128`；`output_fields=[id, page_id, chunk_index, chunk_text]`；不返回 `embedding`
- COSINE 语义：返回值 **越大越相似**（1.0 = 完全相似），结果按 similarity 降序

### 4.3 双系统映射

```
documents.id  ──1:1──►  page_id
chunk_count   ──对应──►  page_id 下 chunk 数量
PK: {page_id}_{chunk_index} 天然按文档聚簇
```

该映射消除"业务 ID ↔ 向量 ID"转换层，删除/重试/检索均可用 `page_id = document_id` 直查。

## 5. Document 生命周期状态机

```
                    ┌──────────────┐
                    ▼              │
        ┌──────────────────────┐   │
        │      PENDING         │   │
        └──────────┬───────────┘   │
                   │ ingest        │
                   ▼               │
        ┌──────────────────────┐   │
        │     PROCESSING       │   │
        └────┬─────────┬───────┘   │
             │         │          │
     成功    ▼         ▼ 失败      │
   ┌───────────┐  ┌───────────┐   │
   │  SUCCESS  │  │  FAILED   │◄──┘  （重试：FAILED → PROCESSING）
   └───────────┘  └───────────┘
        │ 删除         │ 删除
        └─────┬────────┘
              ▼
        ┌───────────┐
        │  DELETING │  并发互斥 gate：
        └───────────┘  ingest/retry 在 DELETING 下被拒绝
        │ MySQL 行删除成功
        ▼
     「不存在」（幂等终态，DELETE 返回 204）
```

状态集合（`DocumentStatus`）：`PENDING / PROCESSING / SUCCESS / FAILED / DELETING`

- `DELETING` 为删除已启动但未提交的中间状态，用于 Delete ↔ Ingest/Retry 并发互斥
- `FAILED` 状态可经 `POST /documents/{id}/ingest` 重试（进入 PROCESSING 时清空旧 `error_message`）

## 6. 核心业务流程

### 6.1 上传（POST /documents/upload）

```
读取 multipart 文件
→ 输入校验（文件名非空、无路径分隔符、扩展名受支持、大小 ≤ MAX_PAGE_CONTENT_BYTES）
→ LocalFileStorage.save() 落盘
→ create_document(PENDING, chunk_count=0)
→ TextParser.parse() 解析文本
→ RecursiveChunker.split() 切块（CHUNK_SIZE=700 / OVERLAP=100）
→ DocumentIngestService.ingest_document()（复用 6.2 流程）
→ SUCCESS（响应含 id / filename / file_size / mime_type / status / chunk_count）
```

失败策略：**任一环节失败保留已保存文件（便于 retry）**，Document 置 `FAILED` + `error_message`（截断 2048），原始异常继续上抛由全局 handler 映射（400/413/415/500/502/503）。

### 6.2 Document ingest / 重试（POST /documents/{id}/ingest）

```
Step 1   get_document(document_id)                不存在 → DocumentNotFoundError(404)
Step 1.5 DELETING gate                            DELETING → 拒绝（DocumentOperationError, 503）
Step 2   update_status(PROCESSING, 清空旧 error)  进入 PROCESSING 不修改 chunk_count
Step 3   ingest_service.ingest_page(page_id, chunks)
         ├── query old_ids                        现有 chunk ID 集合
         ├── embedding + 构造 ChunkVector + upsert（按 PK 幂等覆盖）
         └── delete stale_ids（差集）              re-ingest 收敛
Step 4   update_ingest_result(SUCCESS, chunk_count)  或
         update_failure(FAILED, error_message)      异常继续上抛
```

### 6.3 删除（DELETE /documents/{id}，幂等）

```
Step 1  get_document(document_id)     不存在 → 直接视为完成（204）
Step 2  PROCESSING gate               PROCESSING 状态下拒绝删除
Step 3  update_status(DELETING)       DELETING 作为并发互斥标记
Step 4  Milvus query_page_chunks(page_id) → delete_chunks(ids)
        每次删除都重新 query（不假设上次删除完全生效，保证收敛）
Step 5  FileStorage 删除物理文件
Step 6  MySQL delete_document 行删除   MySQL 删除为最终提交点
```

### 6.4 RAG 检索（POST /rag/search）

```
query → EmbeddingClient.embed([query])
→ Milvus search(vector, limit=candidate_limit=max(limit,10), ef=128)
→ 候选结果按 page_id 批量反查 documents（get_documents_by_ids）
→ post-filter：仅保留 status == SUCCESS（孤儿 chunk 一并过滤）
→ 截取前 limit 条，附 Document 元数据（document_id/filename/status/created_at）
→ 返回（COSINE similarity 降序，最相似在前）
```

RAG 检索结果字段（9 项）：`id / page_id / chunk_index / chunk_text / distance / document_id / filename / status / created_at`。其中前 5 项来自 Milvus DTO（`ChunkSearchResult`），后 4 项为 Document 元数据扩展；`document_id == page_id`；不含 embedding 与 `file_path`。

## 7. 三方一致性设计

| 系统 | 角色 | 一致性职责 |
|---|---|---|
| MySQL `documents` | 状态权威 | 生命周期状态、chunk_count、error_message 的唯一事实源 |
| Milvus `page_chunks` | 向量载体 | 可查询即应被检索；孤儿 chunk（对应 Document 非 SUCCESS）在 RAG post-filter 过滤 |
| FileStorage `uploads/` | 原始文件 | 上传落盘；删除时清理；失败时保留（支持 retry） |

一致性规则：

1. **检索过滤在应用层**：Milvus 只做相似度 Top-K，SUCCESS 过滤与元数据补齐在 MySQL 反查后完成（孤儿 chunk 天然被滤除）。
2. **删除顺序固定**：Milvus → 文件 → MySQL 行，MySQL 行删除为最终提交点；任一步失败，重发 DELETE 可收敛（幂等）。
3. **上传失败保留文件**：Document 为 FAILED 但文件仍在，可经 ingest 重试复用，避免重复上传。

## 8. 异常体系与 HTTP 映射

**异常层次**（`core/exceptions.py`）：

- 文档领域：`DocumentUploadError` 族 / `DocumentStorageError` 族 / `DocumentParserError` 族 / `DocumentChunkingError` 族 / `DocumentNotFoundError` / `DocumentOperationError`
- 外部服务：`MilvusRepositoryError`（→ `MilvusConnectionError` / `MilvusOperationError` / `MilvusSchemaMismatchError`）、`EmbeddingClientError`（→ `EmbeddingConfigError` / `EmbeddingAPIError` / `EmbeddingResponseError`）

**全局映射**（`main.py` exception handlers，Router/Service 不吞异常）：

| 异常 | HTTP | 语义 |
|---|---|---|
| `DocumentNotFoundError` | 404 | 资源不存在，不可重试 |
| `DocumentFileTooLargeError` | 413 | 超过 `MAX_PAGE_CONTENT_BYTES` |
| `DocumentUnsupportedExtensionError` | 415 | 仅 `.txt/.md/.markdown` |
| `DocumentFileEmptyError` / 其他 `DocumentUploadError` | 400 | 客户端输入问题 |
| `DocumentStoragePathTraversalError` | 400 | 路径穿越（深层防御） |
| 其他 `DocumentStorageError` | 500 | 磁盘 IO 失败，可重试 |
| `DocumentParserUnsupportedExtensionError` | 400 | 防御兜底 |
| 其他 `DocumentParserError` | 500 | 读取/解码失败，可重试 |
| `DocumentChunkingError` | 500 | 切分配置/内部错误 |
| `DocumentOperationError` | 503 | MySQL 数据服务异常，可重试 |
| `MilvusRepositoryError` | 503 | Milvus 异常，可重试 |
| `EmbeddingClientError` | 502 | 百炼异常，可重试 |

未知异常不吞，由 FastAPI 默认转 500。

## 9. 幂等性与并发控制

| 场景 | 机制 |
|---|---|
| Milvus 初始化 | `MilvusInitializer.initialize()`：Collection 存在则跳过，不删重建（lifespan startup 调用） |
| chunk 写入 | `upsert` 按 PK `{page_id}_{chunk_index}` 覆盖，重复 ingest 结果一致 |
| re-ingest | query old → upsert new → delete stale 差集，多次调用收敛 |
| 删除 | 幂等 204；每次删除重新 query；MySQL 行删除为最终提交点 |
| 删除并发写 | `DELETING` gate：ingest/retry 遇 DELETING 直接拒绝，不触碰 Milvus、不 Embedding |
| PROCESSING gate | 删除遇 PROCESSING 拒绝，防止删除进行中的文档正在 ingest |
| 重试 | `FAILED → PROCESSING`（清空旧 error）→ `SUCCESS/FAILED`；`chunk_count` 在失败路径保持旧值 |

## 10. 依赖注入设计（core/di.py）

全部依赖通过 `Depends(get_*)` 工厂装配，Router 不直接实例化 Repository/Service：

- `get_settings`：配置单例（pydantic-settings，读取 `.env`）
- `get_milvus_initializer`：lifespan 专用（不走 Depends）
- `get_document_repository` / `get_document_ingest_service` / `get_document_upload_service` / `get_document_delete_service` / `get_ingest_service` / `get_rag_service`

Service 构造时接收 Protocol 依赖；测试通过注入 mock 实现验证业务编排。

## 11. 配置体系

- 配置加载：pydantic-settings，读取项目根 `.env`（模板 `.env.example`）
- 分组：应用（`APP_ENV/API_HOST/API_PORT/API_TOKEN`）、MySQL、Redis（预留）、Milvus、百炼（`BAILIAN_*`）、上传（`UPLOAD_DIR/MAX_PAGE_CONTENT_BYTES`）、Ingest/RAG（`CHUNK_SIZE/CHUNK_OVERLAP/EMBEDDING_BATCH_SIZE/RAG_TOP_K/...`）、CORS（预留）
- 关键约束：
  - `BAILIAN_EMBEDDING_DIMENSION` 必须与 Milvus `embedding.dim` 一致；修改需重建 Collection 并全量 re-ingest（`FLOAT_VECTOR.dim` 创建后不可变）
  - `EMBEDDING_BATCH_SIZE` 不得超过百炼硬限制 10
  - `RAG_TOP_K` / `INGEST_MAX_RETRIES` / `PROCESSING_TIMEOUT_SECONDS` / `API_TOKEN` / `CORS_ORIGINS` 为预留配置，当前实现不读取

## 12. 测试与验证

- 框架：unittest（13 个测试文件，位于 `backend/tests/`）
- 覆盖：
  - API 层：document / upload / ingest / rag 端点契约
  - Service 层：上传失败策略、状态机修复回归、重试生命周期、DELETING gate、幂等删除收敛、RAG 后置过滤
  - 数据层：DocumentRepository（SQLite 真实实现回归）
  - 组件：Parser / Chunker / FileStorage / EmbeddingClient
- 验证命令：`.venv\Scripts\python.exe -m unittest discover -s backend/tests`
- 实测基线：201 个测试用例全部通过（Phase 2.14 验证）

## 13. 关键设计决策记录

| 决策 | 结论 | 理由 |
|---|---|---|
| Document 主键与 Milvus 关系 | `document.id = page_id` 1:1（方案 A） | 消除双 ID 转换，删除/重试/检索直查 |
| Milvus PK 格式 | `{page_id}_{chunk_index}` | 天然按文档聚簇；upsert 幂等 |
| 向量索引 | HNSW + COSINE，M=16，efConstruction=200 | 高召回 + 内存可控；相似度语义直观（1.0 自相似） |
| 检索过滤位置 | 应用层 post-filter（非 Milvus 表达式） | 孤儿 chunk 过滤 + 元数据补齐需 MySQL 反查，一次完成 |
| 状态存储 | `String(32)` + 应用层常量（非 SQLAlchemy Enum） | 避免 migration 痛点 |
| 重试入口 | 复用 `POST /documents/{id}/ingest`（FAILED → PROCESSING） | 单一 ingest 路径，避免重复实现 |
| 删除顺序 | Milvus → 文件 → MySQL | MySQL 行删除为最终提交点，可幂等收敛 |
| 异常策略 | 领域异常 + 全局 handler，不吞异常 | 分层职责清晰，可观测性完整 |

## 14. 已知边界与演进方向

**当前边界**（如实记录，供面试陈述引用）：

- 浏览器扩展端尚未实现：`extension/` 目录目前仅作为未来插件端的结构占位，无实际采集/剪藏业务代码；当前数据入口为后端文件上传 API
- 仅文本解析（`.txt/.md/.markdown`）；无 PDF/DOCX/OCR/MinerU
- 同步 ingest，无异步队列；上传大文件会阻塞请求
- 无鉴权、无 CORS 中间件接入（配置已预留）
- 无 user/多租户
- Redis 已部署未接入业务；`RAG_TOP_K` 等为 future config

**演进方向**（未实现）：浏览器扩展端（网页采集 / 剪藏入口，`extension/` 由占位变为实际实现）、异步 ingest（Redis 队列）、PDF/DOCX 解析、鉴权与租户隔离、基于 `qwen-plus` 的生成式问答。
