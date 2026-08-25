# web-rag-clipper-full — Phase 2.3 Milvus Repository 接口设计

## 阶段约束

> 本阶段**仅设计 Milvus Repository 接口契约**（Protocol / DTO / 异常 / 方法语义），作为 Phase 2.4 落地的唯一依据。
> **严格禁止**：创建 Milvus Client、连接 Milvus、创建 Collection、实现 pymilvus 调用代码、实现 Repository Impl / Service / API；不得修改 PHASE0/2.1/2.2 文档。
> 依据：PHASE0_ARCHITECTURE.md · PHASE2_DATA_MODEL.md · PHASE2_MILVUS_SCHEMA.md · `backend/requirements.txt` · `.env.example`

> **当前实现注记（Phase 2.12 Step 4）**：
> 本文档为 Phase 2.3 **历史设计**。当前实现：`page_id` 对应 **`Document.id`**（document.id = Milvus.page_id 1:1，
> 不再存在 `pages` 表）；RagService 按 **`documents.status == SUCCESS`** 做 post-filter；
> Milvus COSINE 实际返回 **similarity（余弦相似度）**，数值越大越相似，结果按相似度**降序**返回
> （历史设计所述 distance ASC 不成立，详见 §7 参数约束行内注记）。

---

## 1. Repository 职责

### 1.1 支持能力（仅 4 种 Milvus 数据访问）

Collection 固定为配置注入的 `MILVUS_COLLECTION`（默认 `page_chunks`，**禁止在代码中硬编码集合名**）。

| 方法 | 对应 Milvus 族 | 作用 |
|------|--------------|------|
| `query_page_chunks` | query（标量过滤+投影） | re-ingest 按 `page_id` 取该页所有 chunk 的 PK id 列表（Phase 2.2 §15 Step 4） |
| `upsert_chunks` | upsert（按 PK 覆盖/插入） | 写入 chunk 向量与文本（Phase 2.2 §15 Step 5） |
| `delete_chunks` | delete（按 PK 精确删除） | 删除 stale 差集；**严禁按 page_id 条件删除**（Phase 2.2 §15.7 红线） |
| `search` | ANN search | RAG 问答 top-K 候选召回 |

### 1.2 明确不负责

以下能力**全部不在 MilvusRepository**：文档解析 / Chunk 切分 / Embedding 生成与分批（`EMBEDDING_BATCH_SIZE=10`）/ 业务状态机 / MySQL 事务 / query-upsert-delete 三步流程编排 / API 请求与用户身份 / Collection 创建与索引构建 / 连接与健康检查。

---

## 2. 分层关系

### 2.1 调用链（严格 Phase 0 §4.2 三层）

```
FastAPI API （pages.py / chat.py）
   ↓
Service （IngestService 编排三步 / RagService 过滤取 top-5）
   ↓
MilvusRepository （本文件：仅单次 Milvus 语义操作，不含流程）
   ↓
Milvus Client （pymilvus 2.4.x；Phase 2.2 §2 推荐 2.4.15）
```

### 2.2 依赖原则

- 仅单向依赖：上层知道下层；Repository 永不 import `api` / `services`。
- 依赖倒置：Service 依赖 `MilvusRepository Protocol`（接口），不依赖具体 Impl。
- Collection 名、连接参数一律通过 `core.config.Settings`（读取 `.env`）注入 Impl 构造函数。

---

## 3. Repository Interface

### 3.1 Protocol 定义（Python 3.12+ typing.Protocol；仅签名不实现）

```python
from __future__ import annotations
from typing import Protocol, runtime_checkable
from .dto import ChunkVector, ChunkSearchResult  # 见 §4


@runtime_checkable
class MilvusRepository(Protocol):
    """page_chunks Collection 数据访问协议。实现约束：不吞异常；不感知 MySQL/Redis/业务状态机。"""

    # 重排顺序按 §5 方法语义章节保持一致，便于实现阶段对照。

    # 对应 §5.1 / Phase 2.2 §15 Step 4
    def query_page_chunks(self, page_id: int) -> list[str]: ...

    # 对应 §5.2 / Phase 2.2 §15 Step 5
    def upsert_chunks(self, chunks: list[ChunkVector]) -> None: ...

    # 对应 §5.3 / Phase 2.2 §15 Step 7；按 PK ids 精确删除，严禁 page_id 条件删除
    def delete_chunks(self, ids: list[str]) -> None: ...

    # 对应 §5.4 / Phase 2.2 §14.1；metric=COSINE/ef=128/limit=10 默认；dim=1024 强校验；禁返回 embedding
    def search(self, vector: list[float], *, limit: int = 10, ef: int = 128) -> list[ChunkSearchResult]: ...
```

### 3.2 方法说明（合并速查）

| 方法 | 输入 | 输出 | 核心行为 | 异常 |
|------|------|------|----------|------|
| `query_page_chunks(page_id)` | `page_id:int >=0` | `list[str]`（PK id 列表；空=无 chunk，非错） | 用 `page_id INVERTED` 索引，仅投影 `id` 字段 | MilvusConnectionError / MilvusOperationError |
| `upsert_chunks(chunks)` | `list[ChunkVector]`；允许空（直接跳过） | `None` | 按 PK 覆盖或插入；DTO 契约校验确保字段与 Schema 一致 | MilvusOperationError / MilvusSchemaMismatchError（维度/字节/字段缺） |
| `delete_chunks(ids)` | `list[str]`；**空列表立即 return，不发请求** | `None`；不存在 PK 视为成功 | 仅按 `id in [...]` 精确删除；**禁止按 page_id 条件删** | MilvusConnectionError / MilvusOperationError |
| `search(vector, limit=10, ef=128)` | `vector:list[float] len==1024`；limit/ef 可覆盖 | `list[ChunkSearchResult]` 按 COSINE similarity **降序**（最相似在前；历史设计写 distance ASC，实际 pymilvus 2.4.15 返回余弦相似度，自相似=1.0，见 Phase 2.12 Step 4） | 固定：`metric_type="COSINE"` + `output_fields=[id,page_id,chunk_index,chunk_text]`；**严禁返回 embedding** | MilvusSchemaMismatchError（len≠1024）/ MilvusConnectionError / MilvusOperationError |

---

## 4. DTO 设计

DTO 字段与 Phase 2.2 §6 Collection 字段**一一对应**（不多余、不缺失）；实现期建议用 Pydantic v2 承载字段校验。

### 4.1 ChunkVector（写入用；id/page_id/chunk_index/chunk_text/embedding）

| 字段 | 类型 | 约束 | 对应 Milvus 字段 |
|------|------|------|-----------------|
| `id` | `str` | `^\\d+_\\d+$`；长度 ≤ 64；与 `page_id/chunk_index` 推导出的 `f"{pid}_{cid}"` 必须一致 | `id VARCHAR(64) PK auto_id=False`（Phase 2.2 §7） |
| `page_id` | `int` | `>= 0` | `page_id INT64`（Milvus 无 BIGINT UNSIGNED） |
| `chunk_index` | `int` | `>= 0`；单页内从 0 递增 | `chunk_index INT64` |
| `chunk_text` | `str` | 非空；**UTF-8 字节长度 ≤ 4096** | `chunk_text VARCHAR(4096)`（Phase 2.2 §9.3） |
| `embedding` | `list[float]` | 长度 **必须 == 1024**（百炼 v3 dimensions=1024）；无 NaN/Inf | `embedding FLOAT_VECTOR dim=1024`（Phase 2.2 §4） |

```python
# 设计级示例（仅字段与校验意图，非本阶段落地代码）
from pydantic import BaseModel, Field, field_validator

class ChunkVector(BaseModel):
    id:          str       = Field(..., min_length=1, max_length=64, pattern=r"^\d+_\d+$")
    page_id:     int       = Field(..., ge=0)
    chunk_index: int       = Field(..., ge=0)
    chunk_text:  str       = Field(..., min_length=1)
    embedding:   list[float] = Field(..., min_length=1024, max_length=1024)

    @field_validator("chunk_text")
    @classmethod
    def _utf8_bytes_le_4096(cls, v: str) -> str:
        if len(v.encode("utf-8")) > 4096:
            raise ValueError("chunk_text UTF-8 bytes > 4096")
        return v

    @field_validator("id")
    @classmethod
    def _id_matches_fields(cls, v: str, info) -> str:
        d = info.data
        expected = f"{d.get('page_id')}_{d.get('chunk_index')}" if d.get("page_id") is not None and d.get("chunk_index") is not None else None
        if expected and v != expected:
            raise ValueError(f"id={v} != expected {expected}")
        return v
```

### 4.2 ChunkSearchResult（检索用；**不含 embedding**）

| 字段 | 类型 | 约束 | 用途 |
|------|------|------|------|
| `id` | `str` | ≤ 64 | 调试/日志 |
| `page_id` | `int` | `>= 0` | 反查 MySQL title/url/status 构造 citation |
| `chunk_index` | `int` | `>= 0` | 引用排序/调试 |
| `chunk_text` | `str` | 非空 | LLM context 拼接原文 |
| `distance` | `float` | COSINE 值域 **[0.0, 2.0]**（0=完全相同） | 阈值/排序；越小越相似 |

```python
# 设计级示例
class ChunkSearchResult(BaseModel):
    id:          str
    page_id:     int
    chunk_index: int
    chunk_text:  str
    distance:    float = Field(..., ge=0.0, le=2.0)
```

> 红线：`embedding` 字段 **不进入 ChunkSearchResult**；`output_fields` 固定不包含它（Phase 2.2 §13.1）。

---

## 5. 方法语义

### 5.1 query_page_chunks(page_id:int) -> list[str]

- 场景：Phase 2.2 §15 Step 4；也用于 reconcile 时 `len(query_page_chunks(pid))` 与 MySQL `chunk_count` 对账。
- 幂等：天然读幂等。空 list 不视为错误（首次 ingest 空）。
- 过滤表达式：`filter = f"page_id == {page_id}"`；`output_fields = ["id"]`。

### 5.2 upsert_chunks(chunks: list[ChunkVector]) -> None

- 场景：Phase 2.2 §15 Step 5；空 chunks 直接返回（不向 Milvus 发请求）。
- 幂等：**完全幂等**（按 PK id 覆盖，重复调用等价于 1 次）。
- 契约校验：DTO 构造期应已通过 `chunk_text<=4096字节 / embedding.len==1024 / id=={pid}_{cid}`；Impl 期仍建议二次防御，失败抛 `MilvusSchemaMismatchError`。

### 5.3 delete_chunks(ids: list[str]) -> None

- 场景：Phase 2.2 §15 Step 7 删除 stale_ids；及 page 物理删除后的 Milvus chunks 清理。
- 幂等：**完全幂等**；删除不存在的 id 视为成功；空 ids 立即返回。
- 红线：Impl **严禁**出现 `filter = f"page_id == {x}"` 形式的 delete；删除一律使用 `expr = f"id in [...]"` 精确按 PK。

### 5.4 search(vector, *, limit=10, ef=128) -> list[ChunkSearchResult]

- 场景：Phase 2.2 §14.1 RAG 候选召回；后续 RagService 负责：批查 MySQL status → 丢弃非 SUCCESS → 再保留 top-5（`RAG_TOP_K=5`）。Repository **不做 status 过滤**（Milvus 不存 status，过滤必须在应用层）。
- 锁定常量：`metric_type="COSINE"`；`params={"ef": ef}`；`output_fields=["id","page_id","chunk_index","chunk_text"]`。
- 幂等：无状态读幂等。
- 校验：`len(vector) != 1024` → `MilvusSchemaMismatchError`（不可重试）。

---

## 6. Re-ingest 支持

三步顺序**严格 Phase 2.2 §15（upsert-then-delete-stale）**，由 Service 编排；MilvusRepository 只提供三个原子方法，不自己做流程循环或差集计算。

```
Service（IngestService）负责：
  new_ids = {c.id for c in new_chunks}
  old_ids = set(repo.query_page_chunks(page_id))         # Step 4
  stale_ids = old_ids - new_ids
  repo.upsert_chunks(new_chunks)                         # Step 5（失败旧数据不变，Milvus 单次原子）
  repo.delete_chunks(sorted(stale_ids))                  # Step 7（仅 PK 差集）
```

- upsert 失败 → Milvus 状态保持 upsert 前（单次调用原子）；Service 任务内重试。
- delete 失败 → Milvus 状态 =「新 chunks 已写 + stale 残留」，这是**允许中间态**（当前实现：RagService 按 MySQL `documents.status != SUCCESS` 过滤，不会泄漏给 LLM；历史设计为 `pages.status`）；下次 retry 重算差集二次删除。

---

## 7. Search 参数约束

| 参数 | 值/规则 | 可调？ |
|------|---------|--------|
| collection | 读取 `MILVUS_COLLECTION` 注入（默认 `page_chunks`） | 否（配置期改） |
| metric_type | **COSINE**（Phase 2.2 §11 历史定义 distance ∈ [0,2]；**实际返回 COSINE similarity ∈ [-1,1]，越大越相似，1.0 = 完全相似**，Phase 2.12 Step 4 实证） | 否 |
| limit | 默认 **10**（RAG_TOP_K_CANDIDATES） | 可通过关键字参数覆盖 |
| ef | 默认 **128**（HNSW 查询候选池） | 可通过关键字参数覆盖 |
| output_fields | `[id, page_id, chunk_index, chunk_text]` | 否（禁改） |
| embedding 返回 | **严禁**（DTO 无该字段 / output_fields 不含） | 否 |
| vector 维度 | 必须 **1024**（与 `BAILIAN_EMBEDDING_DIMENSION=1024` 严格一致） | 否；不符则 Schema 错，需重建 Collection 并重 ingesting |

---

## 8. 异常设计

所有 Milvus 相关错误统一抛出（Impl 内部不吞；保留 `__cause__` 异常链）：

```
Exception
 └─ MilvusRepositoryError（根类，便于统一兜底）
     ├─ MilvusConnectionError   # 连接/超时/gRPC 断，通常可重试
     ├─ MilvusOperationError    # Milvus 执行失败（集合不存在/索引未就绪/内部错）
     └─ MilvusSchemaMismatchError # 契约错：dim≠1024/chunk_text超4096/字段缺/id不一致；不可重试
```

| 方法 | 可抛派生异常 |
|------|-------------|
| query_page_chunks | MilvusConnectionError / MilvusOperationError |
| upsert_chunks | MilvusConnectionError / MilvusOperationError / MilvusSchemaMismatchError |
| delete_chunks | MilvusConnectionError / MilvusOperationError |
| search | MilvusConnectionError / MilvusOperationError / MilvusSchemaMismatchError |

```python
# 设计级示例（放置建议：backend/repositories/milvus/exceptions.py）
class MilvusRepositoryError(Exception):
    """Milvus Repository 异常根类。"""

class MilvusConnectionError(MilvusRepositoryError):
    """连接失败；通常可重试。"""

class MilvusOperationError(MilvusRepositoryError):
    """操作失败；按场景重试或转 FAILED。"""

class MilvusSchemaMismatchError(MilvusRepositoryError):
    """数据契约不一致（dim/chunk_text/字段）；不可重试。"""
```

---

## 9. 幂等性设计

4 个方法必须全部幂等，才能支撑 IngestService 任务内 3 次重试与超时补偿重试：

| 方法 | 幂等机制 |
|------|----------|
| query_page_chunks | 纯读；无副作用 |
| upsert_chunks | 按 PK id 覆盖；同 (page_id, chunk_index) N 次重写不产生重复 |
| delete_chunks | 空 ids 跳过；按 PK 删除不存在 id 视为成功；**条件删不幂等，因此被禁止** |
| search | 纯读；相同参数/相同 Collection 状态快照结果一致 |

---

## 10. 阶段完成状态

### 已确认契约

- Repository 职责边界（§1）与分层关系（§2）
- Protocol 4 方法签名与固定参数（§3）
- DTO：ChunkVector / ChunkSearchResult 字段与约束（§4）
- 方法语义 + 幂等 + 异常（§5、§8、§9）
- Re-ingest upsert-then-delete-stale 三步顺序（§6）
- Search COSINE/ef=128/limit=10/禁返回 embedding/vector dim=1024（§7）

### 未实现（Phase 2.4+ 落地）

- MilvusInitializer（创建 Collection / 索引，按 Phase 2.2 §18 伪代码）
- PyMilvusRepositoryImpl：Protocol 的 pymilvus 实现 + 异常包装 + 红线校验
- IngestService / RagService 编排（含切分、百炼 embedding、任务内 3 次重试、MySQL 状态流转、RAG status post-filter 取 top-5）
- FastAPI 路由装配与依赖注入
- PROCESSING 超时检测 / retry 接口实现（Phase 0 §6.7）

### Phase 2.2 §19.1 阻塞项回顾

| 项 | 状态 |
|----|------|
| ① dim=1024 写入契约 | ✅ 本设计 DTO + search 校验 + MilvusSchemaMismatchError |
| ② `BAILIAN_EMBEDDING_DIMENSION=1024` env | ✅ `.env.example` 已加；Impl 应读 Settings，改值需重建 Collection 并全量 re-ingest |
| ③ pymilvus 版本收紧为 `==2.4.15` | ✅ `backend/requirements.txt` 已锁定 `pymilvus==2.4.15`（与 docker-compose Milvus Server v2.4.4 同 minor 最终补丁版，禁 2.5.x/3.x） |

---

## Phase 2.3 交付物

- 本文档（`docs/PHASE2.3_MILVUS_REPOSITORY_DESIGN.md`）
- **不新增任何 Python 代码文件**；不修改 Phase 0 / 2.1 / 2.2 文档

---

## 下一阶段 Phase 2.4

顺序建议：
1. 收尾 Phase 2.2 §19.1 #3：收紧 `backend/requirements.txt` 的 pymilvus 版本范围。
2. 实现 MilvusInitializer（按 Phase 2.2 §18 创建 schema/HNSW/page_id INVERTED 索引）。
3. 实现 PyMilvusRepositoryImpl：严格遵守本文件 Protocol、DTO 校验、异常类型、§5.3 禁按 page_id delete、§5.4 禁返回 embedding。
4. 然后进入 IngestService + RagService 编排（Phase 3/5）。
