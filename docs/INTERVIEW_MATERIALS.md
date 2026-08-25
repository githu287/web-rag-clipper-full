# 项目面试与简历材料

> 本材料基于当前实现事实整理，**只包含已验证的能力**（201 个单元测试通过 + 真实环境 E2E 验证），不包含未实现功能。

## 1. 项目一句话定位

**Web RAG Clipper —— 面向网页内容剪藏与知识库构建的 RAG 系统**：产品目标是通过浏览器扩展采集网页内容，送入后端知识库完成解析、切块、向量化与检索；当前阶段已完成 RAG 后端核心链路，浏览器扩展端尚未实现。

## 2. 项目描述（30 秒 / 3 分钟两版）

### 30 秒版

> 我独立设计并实现了 **Web RAG Clipper**——面向网页内容剪藏与知识库构建的 RAG 系统。当前阶段已完成 RAG 后端核心链路：用户上传文档后，系统自动完成文本解析、按 700 字符带 100 重叠递归切块、调用阿里云百炼 text-embedding-v3 生成 1024 维向量并写入 Milvus；检索时用 Milvus HNSW+COSINE 做相似度 Top-K，再反查 MySQL 过滤出 `SUCCESS` 状态的文档并补齐元数据返回。浏览器扩展端（网页采集 / 剪藏入口）尚未实现。整个项目 201 个单元测试全部通过，并在真实 MySQL + Milvus 环境完成端到端验证。

### 3 分钟版

> 项目背景：**Web RAG Clipper** 的产品定位是面向网页内容剪藏与知识库构建的 RAG 系统——通过浏览器扩展采集网页内容，送入后端知识库进行解析、切分、向量化和 RAG 检索。当前阶段代码库的核心实现集中在后端（FastAPI 后端、Document 生命周期、文件上传、Parser/Chunker/Embedding/Milvus/MySQL、RAG Search、Retry/Delete、SUCCESS-only 与孤儿过滤、三方一致性），浏览器扩展端尚未实现，`extension/` 目录仅作为未来插件端的结构占位。
>
> 架构上采用 FastAPI 单体 + 三层结构：API 层只做参数校验和响应构造，Service 层编排业务（上传、ingest、删除、RAG），Repository 层通过 Protocol 接口屏蔽 MySQL 与 Milvus 的具体实现，依赖全部由 DI 工厂装配，这样测试时可以注入 mock，201 个单测不需要真实数据库。
>
> 最核心的设计是**双存储一致性**：MySQL `documents` 表保存文档生命周期状态（PENDING/PROCESSING/SUCCESS/FAILED/DELETING）和 chunk_count 等元数据，是状态权威；Milvus `page_chunks` Collection 只存向量数据，主键直接复用 `{document_id}_{chunk_index}`，实现了文档 ID 与向量分页 ID 的 1:1 映射，消除了双系统 ID 转换层。
>
> 关键流程：上传时同步走「解析 → 切块 → 向量化 → 入库 → 置 SUCCESS」完整链路，任一环节失败则文档置 FAILED 但保留原始文件，可通过 ingest 接口直接重试。RAG 检索先用 Milvus 做相似度 Top-K（候选数取 max(limit,10)），再批量反查 MySQL 过滤掉孤儿 chunk，最后附上文件名、创建时间等来源元数据返回，保证「检索到的内容一定来自有效文档」。
>
> 一致性保障上，删除按「Milvus 删向量 → 删物理文件 → 删 MySQL 行」顺序执行，以 MySQL 行为最终提交点，支持幂等重发收敛；并用 DELETING 状态作为删除与 ingest 的并发互斥 gate。
>
> 质量方面：13 个测试文件共 201 个单元测试全部通过，覆盖状态机回归、幂等删除、失败重试、并发互斥、异常映射等关键路径；代码中异常采用领域异常 + 全局 handler 统一映射，错误语义可区分「客户端问题（4xx）」「服务可重试（5xx）」。

## 3. 简历条目

### 中文版

**Web RAG Clipper（面向网页剪藏与知识库构建的 RAG 系统）｜独立开发**

- 产品定位：通过浏览器扩展采集网页内容，送入后端知识库完成解析、切分、向量化与 RAG 检索；当前阶段已实现 RAG 后端核心链路，浏览器扩展端尚未实现（`extension/` 为结构占位）
- 基于 FastAPI + SQLAlchemy 2.0 + Milvus 设计并实现 RAG 后端，打通「上传 → 解析 → 切块 → Embedding → 向量入库 → 语义检索」全链路
- 设计 MySQL `documents` 表（状态权威）与 Milvus `page_chunks` Collection（向量载体）的 1:1 一致性映射，主键复用 `{document_id}_{chunk_index}`，实现文档级 upsert 幂等与孤儿 chunk 过滤
- 实现文档生命周期状态机（PENDING/PROCESSING/SUCCESS/FAILED/DELETING），以 DELETING 状态作为删除与 ingest 的并发互斥 gate；失败可重试、删除幂等（204）
- RAG 检索采用 HNSW+COSINE 相似度 Top-K + MySQL 反查 post-filter，返回检索片段与文档来源元数据（document_id/filename/status/created_at）
- 领域异常 + 全局 handler 统一映射（400/404/413/415/500/502/503），错误语义可区分「客户端问题」与「可重试服务异常」
- 以 unittest 建立 201 个单元测试（13 个文件，零第三方测试框架），覆盖状态机回归、幂等删除、重试生命周期、并发互斥等关键路径，全部通过并在真实 MySQL+Milvus 环境完成 E2E 验证

### English

**Web RAG Clipper (RAG System for Web Content Clipping & Knowledge Base) | Solo Development**

- Product vision: a browser extension clips web content into a backend knowledge base for parsing, chunking, embedding and RAG retrieval. Current stage: the RAG backend core pipeline is fully implemented; the browser extension is not yet built (`extension/` is a structural placeholder)
- Designed and implemented the RAG backend on FastAPI + SQLAlchemy 2.0 + Milvus, covering the full pipeline: upload → parse → chunk → embedding → vector insert → semantic retrieval
- Built a 1:1 consistency mapping between the MySQL `documents` table (source of truth) and the Milvus `page_chunks` collection (vector store), reusing `{document_id}_{chunk_index}` as the primary key for idempotent upsert and orphan-chunk filtering
- Implemented a document lifecycle state machine (PENDING/PROCESSING/SUCCESS/FAILED/DELETING) with a DELETING gate for delete-vs-ingest mutual exclusion; failed documents are retryable, deletion is idempotent (204)
- RAG retrieval: HNSW+COSINE top-K similarity search followed by MySQL post-filtering, returning chunk text plus source metadata (document_id/filename/status/created_at)
- Unified domain-exception-to-HTTP mapping (400/404/413/415/500/502/503), distinguishing client errors from retryable service errors
- Established 201 unit tests across 13 files with pure unittest (no third-party test framework), covering state-machine regressions, idempotent deletion, retry lifecycle, and concurrency guards; all passing, plus end-to-end verification on real MySQL + Milvus

## 4. 技术亮点（可量化陈述）

1. **双存储一致性设计**：MySQL 状态权威 + Milvus 向量载体，`document.id = page_id` 1:1 映射，消除双系统 ID 转换，删除/重试/检索均直查
2. **幂等架构**：Milvus upsert 按 PK 覆盖、re-ingest 三步收敛（query old → upsert new → delete stale）、删除幂等 204、Collection 初始化幂等（存在即跳过）
3. **并发互斥**：DELETING 状态 gate 阻止 ingest/retry 与删除竞争；PROCESSING gate 阻止删除进行中文档
4. **检索正确性**：应用层 post-filter 保证检索结果只来自 SUCCESS 文档，孤儿 chunk 天然过滤
5. **工程化质量**：201 单测（13 文件、纯 unittest）+ 真实环境 E2E 验证 + Alembic 迁移管理 + 版本锁定（pymilvus==2.4.15）

## 5. 面试 Q&A

### Q1：为什么 MySQL 和 Milvus 都有文档数据？为什么不只用 Milvus？

Milvus 擅长向量相似度检索，但它是"可丢弃"的数据（重启可从源重建），不适合做唯一事实源。MySQL 保存文档的完整生命周期（状态、chunk_count、错误信息、创建时间），是状态权威；Milvus 只存"可查询的向量数据"。两者互补：查询路径是「Milvus 找相似 → MySQL 反查过滤+补元数据」。

### Q2：为什么 document.id 直接等于 Milvus 的 page_id？

这是本项目最重要的决策。早期方案需要维护「业务 ID ↔ 向量 ID」的映射表，删除、重试、检索都要做两次转换。采用 1:1 映射后，所有按文档维度的操作（删除、重试、孤儿过滤）都可以用 `page_id = document_id` 直接做，少了一层映射、少了一类不一致 bug。代价是 Document 删除前必须保证 Milvus 数据清干净，所以删除顺序固定为「Milvus → 文件 → MySQL」。

### Q3：Milvus 主键为什么设计成 `{document_id}_{chunk_index}`？

三个好处：一是天然按文档聚簇，一个文档的 chunk 前缀相同，删除/重试可以按前缀范围操作；二是 upsert 幂等，同一文档重复 ingest 时按主键覆盖，不会产生重复 chunk；三是 chunk_index 保序，为将来展示原文片段顺序留了基础。

### Q4：为什么选 COSINE + HNSW？

百炼 text-embedding-v3 的向量未归一化，COSINE 度量对向量长度不敏感，语义检索更稳定，且返回 0~1 的相似度语义直观（1.0 表示完全相似）。HNSW 相比 IVF 不需要训练、查询延迟低，适合中小规模数据集（文档 chunk 数量级）；M=16、efConstruction=200 是召回率与内存的平衡点。检索时 ef=128 换取更高召回。

### Q5：RAG 的「SUCCESS 过滤」为什么放在应用层而不是 Milvus 表达式里？

因为过滤条件依赖 MySQL 的 documents 表（status 字段），而 Milvus 与 MySQL 是两个系统，无法在一条查询里做 join。所以采用「Milvus 候选 Top-K（取 max(limit,10)）→ 批量反查 MySQL → 应用层过滤」的两阶段方案。同时这样也顺带补齐了文件名、创建时间等元数据，一次反查解决两个问题。取 max(limit,10) 是为了避免过滤后不足 limit 条。

### Q6：删除为什么按「Milvus → 文件 → MySQL」顺序，而不是反着来？

MySQL 行删除是最终提交点。如果先删 MySQL 行、后删 Milvus 失败，就会出现「MySQL 无记录、Milvus 有孤儿向量」，无法再通过文档维度清理（已经找不到这条文档了）。反过来，先删 Milvus、文件，最后删 MySQL 行——如果中途失败，文档还在 MySQL 里，重发 DELETE 会重新执行剩余清理，幂等收敛。

### Q7：DELETING 状态解决了什么问题？

并发问题：删除过程中 ingest/retry 如果同时进行，可能一边在删除向量、一边在写向量，产生竞态。解决方案是把删除做成有状态的：先置 DELETING（一个原子 UPDATE），ingest/retry 在 DELETING 下直接拒绝返回错误，删除完成后行消失。这是一个简单的乐观互斥：谁先抢到 DELETING 状态谁执行。

### Q8：重试机制是怎么设计的？

失败路径保留了原始文件、Document 置 FAILED 并记录错误摘要。重试直接复用 `POST /documents/{id}/ingest`：从 FAILED 回到 PROCESSING（清空旧错误），重新走解析切块向量化入库，成功则 SUCCESS + 更新 chunk_count，失败则回 FAILED。没有单独的重试端点，保证 ingest 逻辑只有一条实现路径，避免分叉。

### Q9：同步 ingest 的取舍？

当前 ingest 在请求线程内同步执行，优点是逻辑简单、状态机直接、上传即返回结果，测试也容易覆盖。缺点是上传大文档会阻塞请求，且进程崩溃会留下 PROCESSING 状态。演进方向是引入异步队列（Redis + worker），把「置 PROCESSING 即返回」与「后台执行」分离。这是明确记录在路线图中的未实现项。

### Q10：异常体系怎么设计的？

领域异常按「存储/解析/切分/外部服务」分层定义，Router 和 Service 不吞异常，统一由 main.py 的全局 handler 映射为 HTTP。映射原则是**区分错误语义**：4xx 表示客户端问题（400 空文件、404 不存在、413 超限、415 不支持扩展名），5xx 表示服务可重试（502 Embedding 异常、503 Milvus/MySQL 异常）。这样调用方可以根据状态码决定是否重试。

### Q11：测试怎么覆盖「状态机修复回归」这种时序问题的？

单测中注入 mock Repository/Milvus，通过脚本编排调用序列（如先 FAILED 再 retry），断言状态转移与调用顺序。关键路径（上传失败保留文件、DELETING gate、幂等删除收敛、FAILED→PROCESSING 清空错误）都有专门测试。201 个用例全部通过，另在真实 MySQL + Milvus 环境跑通上传→检索 E2E。

### Q12：遇到过什么印象深刻的问题？

pymilvus 版本兼容问题：Milvus 2.4 对应 pymilvus 2.4.x，而 pymilvus 2.5+/3.x 有协议不兼容风险，所以项目把 pymilvus 锁死为 2.4.15，并在文档中明确「禁止跨 minor 升级」。这让我理解了向量数据库生态中 client-server 版本匹配的重要性，以及依赖锁定的工程价值。

## 6. 诚实边界（回答"还有什么没做"时的标准表述）

以下均未实现，面试中应如实说明，避免被追问穿帮：

- 浏览器扩展端（Chrome 插件）尚未实现：`extension/` 目录目前仅作为未来插件端的结构占位，无实际网页采集/剪藏业务代码；当前数据入口仅为后端文件上传 API
- 仅支持 `.txt/.md/.markdown` 文本解析，PDF/DOCX/OCR/MinerU 未实现
- ingest 为同步处理，无异步队列；大文档会阻塞请求
- 无鉴权与 CORS 中间件接入（`API_TOKEN`/`CORS_ORIGINS` 配置已预留）
- 无 user/多租户体系（`user_id` 字段预留）
- Redis 已部署但业务未接入；`RAG_TOP_K`/`INGEST_MAX_RETRIES`/`PROCESSING_TIMEOUT_SECONDS` 为预留配置
- RAG 当前仅检索（返回片段+来源），未接入 `qwen-plus` 做生成式问答

## 7. 关联材料

- 架构设计：`docs/ARCHITECTURE.md`
- 历史演进（设计决策全过程）：`docs/PHASE0_ARCHITECTURE.md`、`docs/PHASE2_DATA_MODEL.md`、`docs/PHASE2_MILVUS_SCHEMA.md`、`docs/PHASE2.3_MILVUS_REPOSITORY_DESIGN.md`
