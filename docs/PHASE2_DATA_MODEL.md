# web-rag-clipper-full — Phase 2.1 MySQL 数据模型设计

> **阶段约束**：本阶段仅进行数据模型设计，不实现 Repository / Service / API / Milvus / Redis 业务代码。
>
> **依据文档**：[PHASE0_ARCHITECTURE.md](./PHASE0_ARCHITECTURE.md)（Phase 0.1 已确认架构）

> **当前实现注记（Phase 2.12 Step 4）**：
> 本文档为 Phase 2.1 **历史数据模型设计**（`pages` / `summaries` / `ingest_attempts` 表）。
> 当前实现已迁移为 **`documents` 表**（Phase 2.9 起），并采用 **`Document.id = Milvus.page_id` 1:1 映射**，
> 系统不再存在 `pages` 表。下文所有 `pages.*` / `pages.id` / `pages.status` 描述均为历史设计，
> 对应关系现由 `documents.status` 承担，不代表当前实现。

---

## 1. ER 关系说明

```
┌─────────────────┐
│     pages       │
│  (网页知识库页面) │
└────────┬────────┘
         │ 1
         │
         ├──────────────────┐
         │                  │
         │ N                │ N
         ▼                  ▼
┌─────────────────┐  ┌──────────────────┐
│   summaries     │  │ ingest_attempts  │
│  (页面摘要)      │  │ (ingest 尝试记录) │
└─────────────────┘  └──────────────────┘
```

| 关系 | 基数 | 说明 |
|------|------|------|
| `pages` → `summaries` | 1 : 0..1 | 第一版每页**最多一条**当前摘要（见 §8 删除策略与摘要方案） |
| `pages` → `ingest_attempts` | 1 : N | 每次 BackgroundTasks ingest 执行产生一条 attempt |
| `pages` ↔ Milvus | 1 : N（逻辑） | Milvus 以 `page_id` 关联 chunks；不在 MySQL 存向量 |

**职责边界：**

- MySQL 仅存业务元数据、处理状态、摘要持久化、ingest 审计轨迹
- Milvus 存 chunk 向量与检索文本；`pages.chunk_count` 记录成功写入 Milvus 的 chunk 数量
- Redis 缓存摘要与幂等锁；**不是**摘要持久化来源

---

## 2. pages 字段设计

| 字段 | 类型 | 空 | 默认值 | 说明 |
|------|------|----|--------|------|
| `id` | `BIGINT UNSIGNED` | NO | AUTO_INCREMENT | 主键；MySQL 8 自增整型，单用户场景足够且 JOIN 高效 |
| `url` | `VARCHAR(2048)` | NO | — | 页面原始 URL；**不作为去重键**；允许同一 URL 不同正文（内容变更） |
| `title` | `VARCHAR(1024)` | NO | `''` | 页面标题；空标题存空字符串 |
| `raw_text` | `MEDIUMTEXT` | NO | — | **规范化后**的正文文本（与 `content_hash` 计算所用文本一致） |
| `content_hash` | `CHAR(64)` | NO | — | 后端 `SHA-256(normalize(content))` 十六进制结果；**去重唯一键** |
| `status` | `ENUM('PROCESSING','SUCCESS','FAILED')` | NO | `'PROCESSING'` | 页面当前 ingest 状态（见 §5） |
| `error_message` | `TEXT` | YES | `NULL` | 最近一次导致 `FAILED` 的错误信息；SUCCESS 时可置 NULL |
| `chunk_count` | `INT UNSIGNED` | NO | `0` | 成功写入 Milvus 的 chunk 数量；SUCCESS 时 > 0（空正文除外，见备注） |
| `created_at` | `DATETIME(3)` | NO | — | 首次创建记录时间（UTC，应用层写入） |
| `updated_at` | `DATETIME(3)` | NO | — | 最后一次元数据/状态变更时间（UTC，应用层维护） |

**字段设计说明：**

1. **`id`**：采用 `BIGINT UNSIGNED AUTO_INCREMENT`，符合 MySQL 8 常见主键实践；单用户知识库无分片需求，整型主键简单高效。
2. **`content_hash`**：固定 64 字符（SHA-256 hex）；建立 **UNIQUE** 索引实现内容去重。
3. **`url`**：长度 2048 覆盖绝大多数 URL；去重不依赖 url。
4. **`raw_text`**：`MEDIUMTEXT` 最大约 16MB，满足 `.env` 中 `MAX_PAGE_CONTENT_BYTES=2097152`（2MB）限制并留余量。
5. **`title`**：1024 字符覆盖极端长标题；插件提取失败时用空字符串。
6. **`status`**：仅表达 ingest 生命周期，不含摘要或 RAG 状态。
7. **`SUCCESS` 语义**：Milvus 中该 `page_id` 的 chunks **已成功写入**；此时 `chunk_count` 应与 Milvus 实际 chunk 数一致。
8. **`error_message`**：可为空；PROCESSING 时通常为 NULL。
9. **`chunk_count`**：PROCESSING 时为 0；SUCCESS 后为实际写入数；FAILED 时保留失败前已写入数（通常为 0，部分写入场景由 reconcile 处理）。
10. **`created_at`**：记录首次 `POST /api/pages` 创建时间，不因 re-ingest 改变。
11. **`updated_at`**：status / error_message / chunk_count / title / url / raw_text 任一变更时更新。

**空正文边界：** 若规范化后正文为空，仍允许创建 page；ingest 可能产生 0 个 chunk，此时 SUCCESS 且 `chunk_count=0` 视为合法边界情况。

---

## 3. summaries 字段设计

| 字段 | 类型 | 空 | 默认值 | 说明 |
|------|------|----|--------|------|
| `id` | `BIGINT UNSIGNED` | NO | AUTO_INCREMENT | 主键 |
| `page_id` | `BIGINT UNSIGNED` | NO | — | 外键 → `pages.id` |
| `summary` | `TEXT` | NO | — | 摘要正文；持久化来源为 MySQL |
| `model_name` | `VARCHAR(128)` | NO | — | 生成摘要使用的百炼模型名（来自 `.env` 配置） |
| `created_at` | `DATETIME(3)` | NO | — | 首次生成摘要时间 |
| `updated_at` | `DATETIME(3)` | NO | — | 最后一次重新生成/更新摘要时间 |

### 第一版摘要方案：**每页仅保留一条当前摘要（UPSERT）**

| 方案 | 第一版选择 | 原因 |
|------|-----------|------|
| 仅当前摘要 | **采用** | 第一版按需摘要，无版本审计需求；与 Redis 缓存一一对应；查询 `WHERE page_id=?` 即可 |
| 保留历史版本 | 不采用 | 增加存储与 API 复杂度；若后续需要审计可新增 `summary_versions` 表 |

**实现语义：**

- 首次 `POST /api/pages/{page_id}/summary` → `INSERT`
- 再次生成摘要 → `UPDATE` 同一条记录的 `summary`、`model_name`、`updated_at`
- 数据库约束：`UNIQUE(page_id)` 保证每页最多一条
- Redis 缓存 miss 时回源 MySQL；regenerate 后需失效 Redis 缓存（Phase 后续实现，本阶段仅设计）

---

## 4. ingest_attempts 字段设计

| 字段 | 类型 | 空 | 默认值 | 说明 |
|------|------|----|--------|------|
| `id` | `BIGINT UNSIGNED` | NO | AUTO_INCREMENT | 主键 |
| `page_id` | `BIGINT UNSIGNED` | NO | — | 外键 → `pages.id` |
| `attempt_no` | `INT UNSIGNED` | NO | — | 该 page 的第 N 次 ingest **任务**（单调递增，见 §9） |
| `status` | `ENUM('RUNNING','SUCCESS','FAILED')` | NO | `'RUNNING'` | 单次 attempt 执行状态（见 §5） |
| `error_message` | `TEXT` | YES | `NULL` | attempt 失败原因；SUCCESS 时为 NULL |
| `started_at` | `DATETIME(3)` | NO | — | BackgroundTasks 开始执行时间 |
| `finished_at` | `DATETIME(3)` | YES | `NULL` | attempt 结束时间；RUNNING 时为 NULL |

**与 pages.status 的区别：**

| 表 | 状态枚举 | 表达对象 |
|----|---------|----------|
| `pages.status` | PROCESSING / SUCCESS / FAILED | 页面**当前** ingest 结果（对外可见） |
| `ingest_attempts.status` | RUNNING / SUCCESS / FAILED | **单次** BackgroundTasks 执行过程（审计/排查） |

**每次 BackgroundTasks ingest 执行**必须：

1. 分配 `attempt_no`（见 §9）
2. `INSERT ingest_attempts` status=`RUNNING`
3. 任务结束时 `UPDATE` 为 SUCCESS 或 FAILED，填写 `finished_at`

**任务内自动重试（最多 3 次指数退避）：**

- 属于**同一次 attempt** 内部行为，**不**产生新的 `ingest_attempts` 行
- 3 次均失败后，该 attempt 标记 FAILED，并同步 `pages.status=FAILED`

---

## 5. 状态机关系

### 5.1 pages.status 状态机

```
                    POST /api/pages（新内容或 re-ingest）
                              │
                              ▼
                       ┌─────────────┐
              ┌───────│ PROCESSING  │◄────── POST /api/pages/{id}/retry
              │        └──────┬──────┘
              │               │
              │    Milvus 写入成功 + 元数据更新
              │               │
              │               ▼
              │        ┌─────────────┐
              │        │   SUCCESS   │
              │        └─────────────┘
              │
              │    ingest 失败 / 任务内重试耗尽 / 超时标记
              │               │
              │               ▼
              │        ┌─────────────┐
              └───────│   FAILED    │──────► retry ──► PROCESSING
                       └─────────────┘
```

### 5.2 ingest_attempts.status 状态机

```
BackgroundTasks 开始
        │
        ▼
   ┌─────────┐
   │ RUNNING │
   └────┬────┘
        │
        ├── Milvus 写入成功 ──► SUCCESS（finished_at 写入）
        │
        └── 失败（含 3 次内部重试耗尽）──► FAILED（finished_at + error_message）
```

### 5.3 两表联动（正常路径）

**成功：**

```
pages.status = PROCESSING
  → INSERT ingest_attempts (attempt_no=N, status=RUNNING)
  → Chunk → Embedding → Milvus upsert 成功
  → UPDATE ingest_attempts SET status=SUCCESS, finished_at=now()
  → UPDATE pages SET status=SUCCESS, chunk_count=K, error_message=NULL, updated_at=now()
```

**失败：**

```
pages.status = PROCESSING
  → INSERT ingest_attempts (attempt_no=N, status=RUNNING)
  → Embedding/Milvus 失败（内部重试 ≤3 次仍失败）
  → UPDATE ingest_attempts SET status=FAILED, error_message=..., finished_at=now()
  → UPDATE pages SET status=FAILED, error_message=..., updated_at=now()
```

**手动 retry：**

```
pages.status = FAILED（或超时后的 FAILED）
  → POST /api/pages/{page_id}/retry
  → UPDATE pages SET status=PROCESSING, error_message=NULL
  → 新 BackgroundTasks → INSERT ingest_attempts (attempt_no=N+1, status=RUNNING)
  → … 同上成功/失败路径
```

**注意：** 同一时刻一个 page 应只有 **0 或 1** 条 RUNNING attempt；并发 retry 由 Redis 锁 + 应用层保证（Phase 后续实现）。

---

## 6. 索引设计

### 6.1 pages

| 索引名 | 类型 | 字段 | 用途 |
|--------|------|------|------|
| `PRIMARY` | 主键 | `id` | 主键查找、外键引用 |
| `uk_pages_content_hash` | UNIQUE | `content_hash` | 内容去重 |
| `idx_pages_status_updated` | 普通 | `(status, updated_at)` | PROCESSING 超时扫描、FAILED 列表 |
| `idx_pages_created_at` | 普通 | `created_at` | 按时间倒序列表 |

**不建索引：**

- `url` 单独索引 — 第一版无「按 URL 精确查重」需求（去重靠 content_hash）；若后续需要「同 URL 历史版本列表」再加 `idx_pages_url(url(255))` 前缀索引

### 6.2 summaries

| 索引名 | 类型 | 字段 | 用途 |
|--------|------|------|------|
| `PRIMARY` | 主键 | `id` | — |
| `uk_summaries_page_id` | UNIQUE | `page_id` | 每页一条当前摘要；兼作 page 查摘要 |

### 6.3 ingest_attempts

| 索引名 | 类型 | 字段 | 用途 |
|--------|------|------|------|
| `PRIMARY` | 主键 | `id` | — |
| `uk_attempts_page_attempt_no` | UNIQUE | `(page_id, attempt_no)` | 防止 attempt_no 重复；并发安全约束 |
| `idx_attempts_page_started` | 普通 | `(page_id, started_at)` | 查看某 page 的 attempt 历史 |

**不建索引：**

- `status` 单独索引 — 第一版 RUNNING 超时扫描可通过 `pages.status=PROCESSING` + `updated_at` 驱动，无需全表扫 attempt status

---

## 7. 外键策略

| 子表 | 外键 | 引用 | ON UPDATE | ON DELETE |
|------|------|------|-----------|-----------|
| `summaries` | `page_id` | `pages(id)` | `CASCADE` | `CASCADE` |
| `ingest_attempts` | `page_id` | `pages(id)` | `CASCADE` | `CASCADE` |

**说明：**

- 第一版 API **暂不暴露** page 删除；外键仍按完整生命周期设计
- 删除 `pages` 行时，关联 `summaries` 与 `ingest_attempts` 级联删除
- Milvus chunks 清理由应用层异步处理（Phase 7 reconcile），不在 MySQL 外键范围

---

## 8. 删除策略

| 对象 | 第一版 | 策略 |
|------|--------|------|
| `pages` | 无删除 API | 表结构预留；未来删除 page 时 CASCADE 子表 |
| `summaries` | 随 page 删除 | `ON DELETE CASCADE` |
| `ingest_attempts` | 随 page 删除 | `ON DELETE CASCADE`；历史 attempt 随 page 一并清除 |
| Milvus chunks | — | page 删除后异步清理（非 MySQL 事务范围） |
| Redis 摘要缓存 | — | page 删除或摘要更新时失效（Phase 后续） |

**摘要 regeneration** 不删除行，仅 UPDATE `summaries` 当前记录。

---

## 9. 重试 attempt_no 策略

### 9.1 分配规则

- `attempt_no` 在**同一 `page_id` 内单调递增**，从 1 开始
- **手动 retry 不从 1 重新开始**，而是 `MAX(attempt_no) + 1`
- 每次 **BackgroundTasks 调度**（含首次 save 触发的 ingest 与 `POST /api/pages/{id}/retry`）各产生 **1 条** 新 attempt

### 9.2 任务内重试 vs 新 attempt

| 行为 | 是否新 attempt 行 | attempt_no |
|------|------------------|------------|
| 同一次 BackgroundTasks 内 Embedding/Milvus 重试（≤3 次） | 否 | 不变 |
| 首次 `POST /api/pages` 触发 ingest | 是 | 1 |
| `POST /api/pages/{id}/retry` 手动重试 | 是 | MAX+1 |
| PROCESSING 超时后再次 retry | 是 | MAX+1 |

### 9.3 并发安全

**问题：** 并发 retry 可能导致 `attempt_no` 重复。

**约束：** `UNIQUE(page_id, attempt_no)` 数据库级防重。

**分配流程（Phase 2.2+ Repository 实现时需遵循）：**

```
BEGIN;
  SELECT id FROM pages WHERE id = :page_id FOR UPDATE;
  SELECT COALESCE(MAX(attempt_no), 0) + 1 AS next_no
    FROM ingest_attempts WHERE page_id = :page_id;
  INSERT INTO ingest_attempts (page_id, attempt_no, status, started_at) ...;
COMMIT;
```

配合 Redis  ingest 锁，保证同一 page 同时只有一个 RUNNING attempt。

---

## 10. MySQL / Milvus 一致性说明

### 10.1 SUCCESS 语义（与 Phase 0.1 一致）

```
pages.status = SUCCESS
```

**当且仅当** 对应 ingest attempt 已成功完成 Milvus upsert，且 `pages.chunk_count` 已更新为实际写入数。

### 10.2 正常 vs 异常

| 场景 | pages.status | 性质 |
|------|-------------|------|
| Milvus 写入成功 → MySQL 更新 SUCCESS | SUCCESS | 正常 |
| Milvus 写入失败 | FAILED | 正常 |
| Milvus 成功 → MySQL 更新 SUCCESS 失败 | PROCESSING 或 SUCCESS（取决于失败点） | **异常**；需 reconcile |
| MySQL SUCCESS 但 Milvus 无 chunks | SUCCESS（错误） | **异常**；reconcile / re-ingest |
| 后端重启，attempt RUNNING 悬挂 | PROCESSING | **补偿**；超时 → FAILED → retry |

### 10.3 数据对应关系

| MySQL | Milvus |
|-------|--------|
| `pages.id` | chunk metadata `page_id` |
| `pages.chunk_count` | 该 `page_id` 下 chunk 条数（应一致） |
| `pages.content_hash` | 不存入 Milvus（去重在 MySQL） |
| `ingest_attempts` | 不存入 Milvus（审计仅在 MySQL） |

**无跨库事务：** ingest 成功路径为先 Milvus 后 MySQL SUCCESS；失败路径 MySQL 必须标记 FAILED。

---

## 11. 最终 MySQL 建表 SQL

> 以下 SQL 为设计产物，**不在本阶段执行**。Phase 2.2 可通过 Alembic migration 落地。

```sql
-- ---------------------------------------------------------------------------
-- web-rag-clipper-full MySQL 8 schema (Phase 2.1)
-- Charset: utf8mb4 | Engine: InnoDB | Time: UTC DATETIME(3)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS `pages` (
    `id`            BIGINT UNSIGNED     NOT NULL AUTO_INCREMENT,
    `url`           VARCHAR(2048)       NOT NULL,
    `title`         VARCHAR(1024)       NOT NULL DEFAULT '',
    `raw_text`      MEDIUMTEXT          NOT NULL,
    `content_hash`  CHAR(64)            NOT NULL COMMENT 'SHA-256 hex of normalized content',
    `status`        ENUM('PROCESSING', 'SUCCESS', 'FAILED')
                                        NOT NULL DEFAULT 'PROCESSING',
    `error_message` TEXT                NULL,
    `chunk_count`   INT UNSIGNED        NOT NULL DEFAULT 0,
    `created_at`    DATETIME(3)         NOT NULL,
    `updated_at`    DATETIME(3)         NOT NULL,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_pages_content_hash` (`content_hash`),
    KEY `idx_pages_status_updated` (`status`, `updated_at`),
    KEY `idx_pages_created_at` (`created_at`)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='网页知识库页面元数据';


CREATE TABLE IF NOT EXISTS `summaries` (
    `id`          BIGINT UNSIGNED   NOT NULL AUTO_INCREMENT,
    `page_id`     BIGINT UNSIGNED   NOT NULL,
    `summary`     TEXT              NOT NULL,
    `model_name`  VARCHAR(128)      NOT NULL,
    `created_at`  DATETIME(3)       NOT NULL,
    `updated_at`  DATETIME(3)       NOT NULL,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_summaries_page_id` (`page_id`),
    CONSTRAINT `fk_summaries_page_id`
        FOREIGN KEY (`page_id`) REFERENCES `pages` (`id`)
        ON UPDATE CASCADE
        ON DELETE CASCADE
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='页面当前摘要（每页一条，UPSERT）';


CREATE TABLE IF NOT EXISTS `ingest_attempts` (
    `id`            BIGINT UNSIGNED   NOT NULL AUTO_INCREMENT,
    `page_id`       BIGINT UNSIGNED   NOT NULL,
    `attempt_no`    INT UNSIGNED      NOT NULL,
    `status`        ENUM('RUNNING', 'SUCCESS', 'FAILED')
                                      NOT NULL DEFAULT 'RUNNING',
    `error_message` TEXT              NULL,
    `started_at`    DATETIME(3)       NOT NULL,
    `finished_at`   DATETIME(3)       NULL,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_attempts_page_attempt_no` (`page_id`, `attempt_no`),
    KEY `idx_attempts_page_started` (`page_id`, `started_at`),
    CONSTRAINT `fk_attempts_page_id`
        FOREIGN KEY (`page_id`) REFERENCES `pages` (`id`)
        ON UPDATE CASCADE
        ON DELETE CASCADE
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='Ingest 尝试记录（每次 BackgroundTasks 一条）';
```

---

## Phase 2.1 交付物

- 本文档（`docs/PHASE2_DATA_MODEL.md`）
- **无业务代码、无 migration 执行、无 Repository 实现**

**下一步（Phase 2.2，需确认后进入）：** Milvus collection schema 设计 + pymilvus/Milvus 版本组合确认。
