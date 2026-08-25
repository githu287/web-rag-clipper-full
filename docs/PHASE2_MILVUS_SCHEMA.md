# web-rag-clipper-full — Phase 2.2 Milvus Collection Schema 设计

> **阶段约束**：仅设计 Milvus Collection 与版本确认；不创建 client、不实现 Service/Repository/API、不改架构文档。
> **依据**：[PHASE0_ARCHITECTURE.md](./PHASE0_ARCHITECTURE.md) · [PHASE2_DATA_MODEL.md](./PHASE2_DATA_MODEL.md) · `docker-compose.yml` · `backend/requirements.txt` · `.env.example`

> **当前实现注记（Phase 2.12 Step 4）**：
> 本文档为 Phase 2.2 **历史设计**。当前实现已统一为 **`Document.id = Milvus.page_id` 1:1 映射**
> （MySQL 使用 `documents` 表，不再存在 `pages` 表）；RAG status 过滤按 `documents.status == SUCCESS`；
> Milvus COSINE 实际返回 **similarity（余弦相似度）**，数值越大越相似，自相似 = 1.0，结果按相似度降序返回。
> 下文所有 `pages.*` / `distance ASC` 描述均为历史设计，不代表当前实现。

---

## 1. Milvus Server 版本（锁定）

| 项 | 值 | 来源 |
|----|----|----|
| 镜像 | `milvusdb/milvus:v2.4.4` | docker-compose.yml |
| 模式 | standalone | docker-compose.yml |
| 端口 | `19530`（gRPC）/ `9091`（health） | docker-compose.yml |
| 依赖 | etcd v3.5.5 + minio RELEASE.2023-03-20 | docker-compose.yml |

本阶段**不改** `docker-compose.yml`。

---

## 2. pymilvus 版本

| Milvus Server | requirements 当前范围 | 官方最终推荐 | 选型 |
|---------------|---------------------|-------------|------|
| **v2.4.4** | `>=2.4.0,<3.0.0` | Milvus 2.4.x install-pymilvus / About 文档推荐 **2.4.15**（同 minor 最后稳定补丁，含全部修复） | **2.4.15** ✅ |

> 不选 2.4.3 / 2.4.4：release notes 里的 SDK 只是「发布时点快照」，非最终推荐；不追求「SDK-Server 版本号一致」而牺牲稳定性。
> 已实施：requirements.txt 已锁定为 `pymilvus==2.4.15`（同 minor 最后稳定补丁）。后续实施阶段需做连接/建库/upsert/delete/search 冒烟测试。

---

## 3. Embedding 模型

| 项 | 值 | 来源 |
|----|----|----|
| 模型 | `text-embedding-v3` | `.env.example` / Phase 0 §8 D |
| 调用 | 百炼 OpenAI 兼容 `/embeddings` | Phase 0 §2 |
| 最大行数（batch 硬限制） | **10** | 百炼文档 ⚠️ |
| 单行 max token | 8192 | 百炼文档 |
| 支持维度 | 1024/768/512/256/128/64（参数 `dimensions`，仅 v3/v4） | 百炼文档 |
| 默认行为 | 不传 `dimensions` → 返回 1024 | 百炼文档 |

> 当前配置已对齐 §19.1 #1/#2 的阻塞性要求：显式锁定 `BAILIAN_EMBEDDING_DIMENSION=1024` 与 `EMBEDDING_BATCH_SIZE=10`（百炼 API 单次请求最大行数硬限制）。

---

## 4. Embedding Dimension

| 项 | 结论 |
|----|------|
| 百炼默认维度 | **1024** |
| 本 Schema 锁定 dim | **1024** |
| Milvus dim 可变吗？ | **否**（Collection 创建后 `FLOAT_VECTOR.dim` 不可变，改 dim 必须：建新 Collection → 全量 re-ingest → 切配置 → 删旧 Collection） |
| .env 对齐要求 | 必须显式加 `BAILIAN_EMBEDDING_DIMENSION=1024`；百炼 API 调用显式传 `dimensions=1024` |

> ⚠️ §4.4（阻塞性待确认，详见 §19.1 #1/#2）。

---

## 5. Collection 名称

统一使用 `.env.example` 中的 **`page_chunks`**（与 Phase 0/2.1 无冲突）。本阶段不改 `.env.example`。

---

## 6. Collection 字段设计

### 6.1 字段总览

| # | 字段 | DataType | PK | nullable | max_length | dim | 作用 |
|---|------|----------|----|----------|------------|-----|------|
| 1 | `id` | VARCHAR | ✅ | NO | 64 | — | 主键：`page_id_chunkIndex`（应用层确定性生成，`auto_id=False`） |
| 2 | `page_id` | INT64 | — | NO | — | — | 历史设计：对应 MySQL `pages.id`；**当前实现（Phase 2.12 Step 4）：对应 `Document.id`（document.id = Milvus.page_id 1:1）**；用于 re-ingest query 旧 IDs 与 RAG 反查 |
| 3 | `chunk_index` | INT64 | — | NO | — | — | page 内从 0 起的 chunk 序号；stale 差集识别 |
| 4 | `chunk_text` | VARCHAR | — | NO | **4096（UTF-8 字节）** | — | chunk 原文；RAG 拼接 LLM context |
| 5 | `embedding` | FLOAT_VECTOR | — | NO | — | **1024** | 百炼向量；dim 创建后不可变 |

### 6.2 字段要点

- **id 长度 64**：`BIGINT(20)_INT(10)` 最长 31 字符，64 足够。
- **page_id 用 INT64**：Milvus 无 `BIGINT UNSIGNED`；单用户 page_id 正数远在 INT64 范围内。
- **chunk_text=4096 字节**（UTF-8）：700 中文字符最坏 2100 字节 + Emoji/罕见字 4 字节最坏 2800 字节，4096 留 1.46× 余量（见 §9）。

### 6.3 不冗余存储的字段

| 字段 | 不存入 Milvus 的原因 |
|------|---------------------|
| url / title | 存 MySQL；RAG 用 `page_id` 反查（§14.2 设计点 1） |
| content_hash | 去重权威在 MySQL |
| status / error_message / chunk_count / created_at / updated_at | 状态与元数据权威在 MySQL；职责边界见 §17.1 表 |

> 不冗余的核心理由：Phase 0 §4.3 已确认 Milvus 不存业务元数据；冗余引入双写一致性复杂度，单用户场景 MySQL 反查代价可忽略。

---

## 7. Primary Key 设计

| Milvus 2.4.4 PK 能力 | 支持 |
|------|------|
| 单字段 PK（INT64 / VARCHAR） | ✅ |
| 复合 PK（多字段联合） | ❌ |
| `auto_id=True/False` | ✅ |

**选型**：`VARCHAR(64)` + `auto_id=False` + **应用层生成**。

> 三个「为什么不」：① 不用复合 PK → Milvus 不支持；② 不用 INT64（`page_id*1e5+chunk_index`）→ 容易溢出或浪费；③ 不用 `auto_id=True` → re-ingest 时无法按 PK 覆盖，必须走 delete+insert，回到禁止方案。
>
> **生成规则**（可解析、调试友好）：
> ```
> id = f"{page_id}_{chunk_index}"      # 示例：page_id=100, i=0  → "100_0"
> ```
> 特性：**确定性**（同一二元组永远同 id → upsert 覆盖）+ **唯一性** + **可反解**。

---

## 8. Upsert 唯一定位策略

Milvus `upsert()` 按 PK `id` 覆盖或插入。这等价于 Phase 0 §6.6 中「upsert by (page_id, chunk_index)」的**逻辑语义**（Milvus 无复合 PK，我们通过字符串拼接压缩到单字段）。

```python
# 数据结构示例（伪代码）
data = [{"id":"100_0","page_id":100,"chunk_index":0,"chunk_text":"...","embedding":[...]}]
client.upsert(collection_name="page_chunks", data=data)
```

---

## 9. chunk_text 长度策略

### 9.1 约束

| 项 | 值 | 来源 |
|----|----|----|
| CHUNK_SIZE | 700 **字符** | `.env.example` |
| CHUNK_OVERLAP | 100 字符 | `.env.example` |
| Milvus VARCHAR max_length 上限 | 65535 **UTF-8 字节** | Milvus 2.4.x |

### 9.2 核心点：字符 ≠ 字节

| 类型 | 单字符 UTF-8 字节 | 700 字符最坏字节 |
|------|-------------------|----------------|
| ASCII | 1 | 700 |
| 中文 CJK | 3 | **2100** |
| Emoji / 罕见 CJK | 4 | **2800** |

> 2100 > 2048，所以 **max_length=2048/1024 直接排除**（会截断纯中文 chunks）。

### 9.3 选型：**max_length = 4096 字节** ✅

- 覆盖 700 纯中文 2100 字节 + 4 字节字符混杂 2800 字节；4096/2800 ≈ 1.46× 余量。
- 覆盖 CHUNK_SIZE 调至 800–1200 字符的未来空间（1200 中文 ≈ 3600 字节）。
- 4096 ≪ 65535，无溢出风险；1 万 chunks × 4KB ≈ 40MB 单用户可接受。

> 不选 8192+：无收益，正常 CHUNK_SIZE 约束已经够用。
> 实施防御：写入 Milvus 前必须校验 `len(chunk_text.encode('utf-8')) <= 4096`，超长截断/拆分并告警。

---

## 10. Vector Index

### 10.1 选型：**HNSW** ✅

| 索引 | 召回 | 延迟 | 适用场景 |
|------|------|------|---------|
| FLAT | 100% 暴力 | 高 | 小数据精确结果 |
| IVF_FLAT | 高 | 中 | 中等数据 nlist/nprobe 调参 |
| **HNSW** | **高** | **低** | **RAG 低延迟高召回（推荐首选）** |
| AUTOINDEX | 黑盒自动 | 黑盒 | 不做精细控制的场景 |

> 不选 AUTOINDEX：Milvus 自动选择=不可控；HNSW 参数 M/efConstruction/ef 有成熟经验值，RAG 场景业内默认。

### 10.2 HNSW 与搜索参数（锁定）

| 维度 | 参数 | 值 | 说明 |
|------|------|----|----|
| 构建 | M | **16** | 每节点邻居数；通用 RAG 默认 |
| 构建 | efConstruction | **200** | 构建候选池；兼顾质量与速度 |
| 查询 | ef | **128** | 查询候选池；Milvus 初始 top-K=10 时 ef≥limit=10，128 留足余量 |

---

## 11. Metric Type

| Metric | 语义 | 需客户端归一化？ | 适配 |
|--------|------|----------------|------|
| **COSINE** ✅ | 1 − cos(θ) ∈ [0,2]（0=完全相同） | **不需要**（Milvus 存储/查询均内部归一化） | **文本语义 embedding 标准** |
| IP（内积） | A·B | 必须归一化，否则模长污染结果 | 推荐系统等已归一化向量 |
| L2 | ‖A−B‖² | 不关心 | 图像/聚类 |

> 选 COSINE 的一句话理由：① 语义相似度只关心方向；② 百炼文档**未明确**保证 text-embedding-v3 返回已归一化 → COSINE 天然避免 IP 的模长污染；③ 结果 [0,2] 可解释；④ 与百炼官方 RAG 示例一致。

---

## 12. Scalar Index

| 字段 | 建索引？ | 类型 | 理由 |
|------|---------|------|------|
| `id` | ✅ 自动 | PK 隐式 | 主键 |
| `page_id` | ✅ 显式 | **INVERTED** | re-ingest Step 4 `query(page_id==x)` + 调试/对账；是 Milvus 端唯二需要建的标量索引 |
| `chunk_index` | ❌ | — | 无单查场景；差集在应用层做集合运算 |
| `chunk_text` | ❌ | — | 第一版不做全文/BM25 混合检索（Phase 0 §7 不引入额外搜索引擎） |
| `embedding` | ✅ 见 §10 | HNSW(COSINE) | 向量检索主索引 |

---

## 13. Search 返回字段

### 13.1 返回（必带）vs 不返回（严禁）

| 字段 | 返回？ | 用途 |
|------|-------|------|
| `id` | ✅ | 调试/日志 |
| `page_id` | ✅ | 反查 MySQL title/url（citation） |
| `chunk_index` | ✅ | 引用排序/调试 |
| `chunk_text` | ✅ | 拼接 LLM context |
| `distance` | ✅ Milvus 自动返回 | score 排序/阈值过滤；COSINE ∈ [0,2] |
| **`embedding`** | **❌ 严禁** | 向量本身不参与 context；避免网络开销 |

### 13.2 Search 参数

```
metric_type: COSINE,  params.ef: 128,  limit: 10
output_fields: [id, page_id, chunk_index, chunk_text]
```

> Milvus 返回 top-10 候选 → 应用层 MySQL status 过滤 → 最终保留 top-5（见 §14.3）。

---

## 14. RAG 查询数据流

### 14.1 流程（7 步）

> 注（Phase 2.12 Step 4）：以下为历史流程描述。当前实现：批查 `documents`（按 `documents.status == SUCCESS` 过滤）；
> Milvus 实际按 COSINE similarity 降序返回（最相似在前），RagService 保持该顺序截取 `limit` 条（候选数 = max(limit, 10)）。

```
POST /api/chat {question}
  → 百炼 embedding (1024 维)
    → Milvus top-10 search (COSINE, ef=128, 无 embedding 返回)
      → 批查 MySQL pages by page_id（id/title/url/status）
        → 应用层丢弃 pages.status != SUCCESS 的所有 chunks  (Milvus 不存 status，过滤不能放 Milvus)
          → 按 distance ASC 保留最多 5 个有效 chunks（= RAG_TOP_K；不足则实际数，0 则返回空）
            → 拼接 context + citations
              → 百炼 qwen-plus → 返回 {answer, citations}
```

### 14.2 关键设计点（4 条）

1. **不冗余 title/url** → 避免双写一致性（§6.3 / §17.1）。
2. **status 过滤必须在应用层** → Milvus/MySQL 非跨库原子，status 权威只在 MySQL。
3. **初始 top-K（10）> 最终 top-K（5）** → 预留过滤余量，防止 SUCCESS pages 不足。
4. **单用户无 tenant 隔离** → Phase 0 §8 E 已确认全库检索。

### 14.3 top-K 参数

| 参数 | 值 | 说明 |
|------|----|----|
| RAG_TOP_K_CANDIDATES | **10**（Milvus 初始） | `.env` 当前可默认；实施期若过滤后经常 <5 可调 15/20 |
| RAG_TOP_K | **5**（最终） | `.env.example` 已有默认值 `RAG_TOP_K=5` |

---

## 15. Re-ingest 流程

### 15.1 触发场景

| 场景 | 触发 | 前置状态 |
|------|------|---------|
| 首次 ingest | `POST /api/pages` | 新建 PROCESSING |
| 手动重试 | `POST /api/pages/{id}/retry` | FAILED |
| 超时补偿后重试 | retry 接口 | PROCESSING 超时 → FAILED → retry |

### 15.2 设计原则 + 禁止项（核心）

**原则**：新版本先写，旧数据最后清。
**禁止流程 ❌**：`delete all old → insert/upsert new`。理由：① 有空窗期；② UPSERT 失败则旧数据**全部丢失**不可回退；③ 有更优方案。
**采用流程 ✅**：**query old IDs → upsert new → delete stale（差集）**。

### 15.3 完整 9 步流程（upsert-then-delete-stale）

```
前置：MySQL pages.status = PROCESSING（首次 / retry 已设）

┌──────────────────────────────────────────────────────────┐
│ Step 1：BEGIN; page FOR UPDATE; MAX(attempt_no)+1;         │
│   INSERT ingest_attempts RUNNING; COMMIT (+Redis 锁)       │
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│ Step 2：Chunk 切分 (700/100)                              │
│   new_ids = {f"{page_id}_{i}" for i in range(N+1)}         │
│   （任务内自动重试 ≤3 次指数退避，不产生新 attempt 行）       │
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│ Step 3：百炼 batch Embedding                              │
│   EMBEDDING_BATCH_SIZE=10   model=text-embedding-v3       │
│   dimensions=1024（显式传入）                              │
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│ Step 4：query 旧 IDs（利用 page_id INVERTED）              │
│   client.query(filter="page_id == {pid}", out=["id"])     │
│   old_ids = set(ids)  （首次 ingest 为空集）               │
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│ Step 5：Milvus upsert 新 chunks（单次 Milvus 调用原子）    │
│   PK 同则覆盖；异则插入；仅 Milvus 内部原子，不延伸跨库。   │
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│ Step 6：判断 Step 5 成功 / 失败（失败 → retry → 仍失败 FAILED）│
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│ Step 7：delete stale by PK id （⚠️ 严禁按 page_id 删）     │
│   stale_ids = old_ids - new_ids                          │
│   空集则跳过（首次 ingest / chunk 数增加的 re-ingest）     │
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│ Step 8：UPDATE pages SET chunk_count=N+1, updated_at=now  │
│   WHERE id=pid   （仅更新数量；此时仍 NOT SUCCESS）         │
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│ Step 9：UPDATE pages SET status=SUCCESS, error=NULL,      │
│   updated_at=now WHERE id=pid;  + 对应 attempt SUCCESS。   │
└──────────────────────────────────────────────────────────┘
```

### 15.4 方案 A/B 对比 + 选型

| 方案 | 流程 | 选 |
|------|------|----|
| A. delete-then-upsert | 先按 page_id 删所有旧 chunks，再 upsert 新 | ❌ 空窗期 + UPSERT 失败丢全部旧数据 |
| **B. upsert-then-delete-stale** | query 旧 IDs → upsert 新 → delete 差集 | ✅ 新数据先写、失败旧数据不丢、空窗极短 |

### 15.5 任务内重试（≤3 次指数退避，同一 attempt 内）

Step 2-9 任一失败从头重跑 2-9（不保留中间结果）。所有操作幂等：
- Step 4 query / Step 5 upsert（按 PK 覆盖）/ Step 7 delete PK / Step 8&9 MySQL 同值更新，均**天然幂等**。
- 重试耗尽 → 统一 `pages.status=FAILED`（见 §17.2 权威表）。

### 15.6 失败处理极简提示 + 一致性权威

- **前提**：Milvus 与 MySQL 无跨库原子事务。
- Step 2-7 失败：走 3 次任务内重试；仍失败则 `pages.status=FAILED`。Step 5 upsert 失败时 Milvus 仍保持旧 chunks（Milvus 单次调用原子性：全成功或全失败）。
- Step 8-9 MySQL 更新失败：可能仍 PROCESSING；**不回滚 Milvus**； reconcile 修复。
- **完整场景表统一在 §17.2.2 / §17.2.3**。

### 15.7 Stale 示例 + 幽灵 chunks RAG 过滤

**示例（page_id=100 重新 ingest 后 chunk 数减少）：**

```
old_ids = {100_0, 100_1, 100_2, 100_3}
new_ids = {100_0, 100_1}
→ stale_ids = {100_2, 100_3}  →  Step 7 按 PK id in ['100_2','100_3'] 删除
                                 （⚠️ 严禁按 page_id 条件删除：会误删 Step 5 刚写入的新版本，
                                  也等于走回禁止的 delete-then-insert 方案）
```

**不变式 3 条：**
1. `pages.status = SUCCESS` ⟺ Step 9 成功执行（且 Step 4-7 Milvus 已全部成功）。
2. Step 4-9 均可重复执行（幂等）。
3. 任何失败路径的 `pages.status` 都不是 SUCCESS；允许中间状态（新+旧 chunks 混合）。

**中间状态为什么不影响 RAG？**

| Milvus 快照 | MySQL pages.status | RAG 检索到？ |
|---|---|---|
| 新 chunks（stale 已清） | SUCCESS | ✅ 通过 |
| re-ingest 中（Step 5 完/7 未完） | PROCESSING | ❌ 应用层按 status 丢弃（§14.2） |
| re-ingest 失败（Step 7 失败） | FAILED | ❌ 同上 |
| 历史遗留（上次失败未重试） | FAILED | ❌ 同上 |

> **与 §17.3 对齐**：Milvus 允许中间不一致；**MySQL `pages.status` 永远是 RAG 返回/retry/展示的唯一权威**。

---

## 17. MySQL / Milvus 一致性策略

### 17.1 职责边界（Phase 0 §4.3 / Phase 2.1 §10.3）

| 数据 | 存储 | 不存储于 |
|------|------|---------|
| url/title/raw_text/content_hash/status/error/chunk_count | MySQL `pages` | Milvus |
| 摘要/ingest 审计 | MySQL `summaries` / `ingest_attempts` | Milvus |
| chunk 向量、chunk_text、page_id、chunk_index | Milvus `page_chunks` | MySQL |
| 摘要缓存、幂等键、ingest 锁 | Redis | MySQL / Milvus |

### 17.2 一致性规则

#### 17.2.1 SUCCESS 语义（Phase 0 §6.6 / Phase 2.1 §10.1）

```
SUCCESS ⟺ attempt 完成 Milvus upsert + stale delete  ∧  pages.chunk_count == 实际写入数
```
唯一成功路径：Milvus upsert OK → stale delete OK → MySQL chunk_count OK → MySQL SUCCESS OK。

#### 17.2.2 正常路径表（成功/失败）

| 场景 | MySQL | Milvus |
|------|-------|--------|
| 正常成功 | SUCCESS, chunk_count=N | N 条新 chunks（stale 清） |
| 失败：Step 2-4 | FAILED, err=... | 旧 chunks 未变 |
| 失败：Step 5 upsert | FAILED, err=... | 旧 chunks 仍在（Milvus 单次调用原子）；新 chunks 未写 |
| 失败：Step 7 delete stale | FAILED, err=... | 新 chunks 已写 + 残留 stale chunks（下次 retry 清） |

#### 17.2.3 异常路径表（需 reconcile）

| 场景 | MySQL | Milvus | 性质 | 处理 |
|------|-------|--------|------|------|
| Milvus 成功 + MySQL 更新失败 | PROCESSING | 新 chunks（stale 清） | **异常** | 启动扫描 PROCESSING 超时 → FAILED → retry（Step 4-9 幂等） |
| MySQL SUCCESS 但 Milvus 缺 chunks | SUCCESS | 空 | **异常** | reconcile/re-ingest（理论不该有；Step 9 仅在 Step 5-7 成功后跑） |
| 后端崩溃导致任务悬挂 | PROCESSING | 中间状态（新+旧或仅旧） | **补偿** | 启动扫描 PROCESSING 超时 → FAILED → retry |

#### 17.2.4 无跨库事务（机制声明）

与 §15.6 一致：两系统间无原子事务。成功路径 = **先 Milvus（upsert + delete stale）后 MySQL SUCCESS**；失败路径 = MySQL FAILED。第一版**不引入**分布式事务/消息队列/2PC，完全靠 retry（任务内 + 手动）+ reconcile 最终一致。

### 17.3 状态权威

- **MySQL**：RAG 过滤 / retry / 对外展示，`pages.status` 唯一依据。
- **Milvus**：纯数据载体，中间不一致由 MySQL 过滤兜底。
- **Redis**：缓存 + 锁，不做权威，重启可重建。

### 17.4 Reconcile 能力边界（Phase 0 §6.7）

第一版必须：
- `POST /api/pages/{id}/retry`（手动重试）
- PROCESSING 超时检测 → 标记 FAILED

第一版可选（启动期轻量扫描）：
- `pages.status='PROCESSING' AND updated_at < now()-timeout` → FAILED

第一版明确**不引入**：Celery / Kafka / RabbitMQ。

---

## 18. Schema 创建参数汇总（设计产物；本阶段不执行）

> 仅设计参考伪代码；Phase 2.3+ Repository 落地。

```python
# Milvus Collection 创建参数（MilvusClient / pymilvus 2.4.x）
from pymilvus import MilvusClient, DataType

schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_fields=False)

schema.add_field("id",          DataType.VARCHAR,      is_primary=True, max_length=64)
schema.add_field("page_id",     DataType.INT64)
schema.add_field("chunk_index", DataType.INT64)
schema.add_field("chunk_text",  DataType.VARCHAR,      max_length=4096)   # UTF-8 字节
schema.add_field("embedding",   DataType.FLOAT_VECTOR, dim=1024)

index = MilvusClient.prepare_index_params()
index.add_index(field_name="page_id",   index_type="INVERTED")
index.add_index(field_name="embedding", index_type="HNSW",
                metric_type="COSINE",   params={"M": 16, "efConstruction": 200})

client.create_collection(collection_name="page_chunks", schema=schema, index_params=index)
```

> search 参数（配套）：`metric_type=COSINE, params={"ef": 128}, limit=10, output_fields=[id,page_id,chunk_index,chunk_text]`。

---

## 19. 当前仍需人工确认的事项

### 19.1 阻塞性（Phase 2.3 进入前必须定案，直接影响 Schema/配置）

| # | 待确认项 | 当前默认设计 | 直接影响 |
|---|---------|-------------|---------|
| 1 | Embedding dimension | **1024**（百炼默认 / 本设计最终选择） | Milvus `embedding.dim` = 1024；改值需重建 Collection 并全量 re-ingest |
| 2 | 新增 `.env` 配置 `BAILIAN_EMBEDDING_DIMENSION=1024`？ | **建议新增** + 百炼 API 调用显式传 `dimensions=1024`（避免依赖默认值产生维度漂移） | 实施阶段 dim 不锁定会导致 Milvus 写入维度不匹配（最严重） |
| 3 | pymilvus 版本实施时是否收紧 requirements？ | **已锁定 `pymilvus==2.4.15`**（backend/requirements.txt）；与 Milvus Server v2.4.4 同 minor 最终补丁版 | 避免未来 pip 解析到 2.5.x 跨 minor 不兼容 |

### 19.2 非阻塞性（实施阶段可调）

| # | 项 | 默认 | 备注 |
|---|----|------|------|
| 4 | chunk_text max_length | 4096 字节 | CHUNK_SIZE 调 1200+ 需重估 |
| 5 | HNSW：M/efConstruction/ef | 16/200/128 | 实施后按召回/延迟实测调 |
| 6 | RAG top-K 候选/最终 | 10 → 5 | 过滤后常 <5 则调大候选 |
| 7 | Milvus 冗余 title/url？ | 不冗余 | 单用户无必要 |
| 8 | re-ingest 方案 | upsert-then-delete-stale ✅ | 已采用 |

### 19.3 明确不在本阶段范围

- Milvus 客户端封装 / Repository / Service / API 实现（Phase 2.3+）
- Alembic migration（Milvus 无 Alembic，Collection 创建由应用启动执行）
- Redis 幂等键/ingest 锁 key 设计；Chunk 切分算法；百炼客户端封装
- CORS / 认证中间件

---

## Phase 2.2 交付物

- 本文档（`docs/PHASE2_MILVUS_SCHEMA.md`）
- **无业务代码 / 无 Collection 创建脚本 / 无 Repository 实现 / 无配置修改**

下一步（Phase 2.3）：用户确认 §19 阻塞性项后，进入 Repository 接口定义或实施阶段。
