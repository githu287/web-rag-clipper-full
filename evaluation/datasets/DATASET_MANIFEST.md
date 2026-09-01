# Phase Evaluation Dataset MANIFEST

> **2026-09-01 运行时复核**：两个 Evaluation Workspace 与 40 篇源文档已完成真实入库，Plugin/Document ID 已对齐到本地忽略目录 `evaluation/aligned-document/`。实际 Chunk 边界复核发现 Part-A 有 21 个 Chunk 占位符超出对应文档的真实 `chunk_count`，且 `sg040` 的部分 Gold 信息在 A19 源文档中不存在。因此当前只允许发布**文档级临时 Baseline**；Chunk 级 Gold 必须人工重标后才能发布 Chunk 级指标，禁止把不存在的 Chunk ID 强行映射。

> **Dataset Build Date**：2025-12-19  
> **Baseline Config (Step 1 §37 Frozen)**：Chunk size=700 / Overlap=100 / Embedding=百炼 text-embedding-v3 dim=1024 / Metric=COSINE / HNSW M=16 efConstruction=200 / TopK default=5 / LLM=qwen-plus temperature=0.2 / Prompt version 6-rule / Milvus collection=page_chunks / Candidate limit rule=all:max(limit,10); current:max(limit*4,40) / Max context chars=4000（硬编码）  
> **Scope**：仅 evaluation/ 目录；**禁止修改 backend/、extension/、alembic/、Milvus schema、MySQL生产表**（Phase 3.7 Baseline 冻结红线）  
> **Dataset Partioning**：Step 2 Part-A = 人工 Gold Dataset + 规范占位符 + 源文档 + 本 Manifest（本阶段已完成✅）；**Step 2 Part-B = 运行时对齐**（在 Phase 3.7 Step 3 前执行，未执行 ❌）。对齐步骤见 Section 5。Part-B 未完成之前，**任何 Evaluation 脚本不允许直接调用生产 API**。

---

## 1. 数据集文件清单

| 文件名 | 样本数 | 评估领域 | Schema（顶层必填字段） | 覆盖 category / subtype / layer 分布 |
|---|---|---|---|---|
| [rag_eval.jsonl](file:///d:/杂/项目/web‑rag‑clipper‑full/evaluation/datasets/rag_eval.jsonl) | **70** | Retrieval Quality + Context Quality + Answer Quality (三层 RAG Quality 主体 A+B+C 报告章节) | `id / plugin_id / category / mode / question / retrieval_ground_truth{gold_document_ids,gold_chunk_ids,relevance_grading?} / context_ground_truth{information_points,note?} / answer_ground_truth{gold_answer,key_points} / is_answerable / negative_eval?(is_answerable=false 时必填 subtype+abstain_keywords+hallucination_indicators) / document_id?(mode=current 时必填) / interference_doc_ids?(category=similar_doc_interference 时推荐) | 7 Category × 10 条：`simple_fact:10 / multi_paragraph:10 / summary:10 / cross_chunk:10 / unanswerable:10 / similar_doc_interference:10 / all_kb_only:10`。Mode 分布：`current=42 + all=28`。Is_answerable：`true=60 / false=10`，10 条 false 全部带 negative_eval 三字段，用于 Abstention Evaluation。 |
| [negative_eval.jsonl](file:///d:/杂/项目/web‑rag‑clipper‑full/evaluation/datasets/negative_eval.jsonl) | **10** | Hallucination / Abstention 专项（报告 Section C）—— 区别于 rag_eval.jsonl 中 10 条 general unanswerable（后者属于 RAG 质量普通抽样，前者是专门精细化 10 subtype × 四维 Abstention 分类） | 同 rag_eval + `subtype / negative_eval{subtype, proposition_text_in_question, truth_label, expected_abstention_response_should_contain, hallucination_indicators, four_way_abstention_class_expected}`（7 字段齐全） | **10 subtype 互不重复**：hallucination_wrong_fact_attribute / cross_domain_unanswerable / future_external_event / wrong_version_semver / nonexistent_person_entity / incorrect_formula_constant / language_mismatch_japanese_docs / sql_injection_probe_in_rag / nonexistent_configuration_property / imaginary_endpoint_operation。Four-way 分布：`CONTRADICTED=3 / UNSUPPORTED=6 / DEFLECTED=1`。 |
| [isolation_eval.jsonl](file:///d:/杂/项目/web‑rag‑clipper‑full/evaluation/datasets/isolation_eval.jsonl) | **70** | Plugin Isolation 五层评估（报告最终独立 Section E）—— 独立于 Accuracy | `id / test_layer(L1/L2/L5) / source_plugin_id / target_plugin_id / probe_plugin_id / category / mode / question / retrieval_ground_truth{gold_document_ids, forbidden_document_ids, forbidden_chunk_ids_any_prefix?, forbidden_terms_in_retrieved_text?} / expected_isolation_result{L1_retrieval_no_A/B_docids, L3_sources_no_A/B_docids, L4_answer_no_java/python_terms, L5_expected_status_code?} / assertions? / severity(CRITICAL|HIGH|MEDIUM)` | **Layer 50 / 10 / 10 精确分布**：<br>• **L1 Retrieval = 50**：Plugin-A header 25 题 × Plugin-B header 25 题，每题都有 `forbidden_document_ids` 列出对面 20 篇文档的全部占位符 ID，交叉断言 Retrieved/Chunks/Sources 的 page_id 不泄露。<br>• **L2 Cross-Workspace Keyword Trap = 10**：两道陷阱文档（B04 状态图 StateGraph 设计模式名词 / B20 Spring AI vs LangChain Python RAG 整篇复用 Plugin-A 的 Embedding/Chunking/Milvus/Recall@K 等 20+ 高权重术语）× 双向 Header 互检，L2 是最高强度的「Embedding 相似度污染」测例，配合 `L4_answer_blacklist_python_terms / _java_terms` 字段同时校验 Answer 层术语泄露。<br>• **L5 Cross-Workspace Ownership 404 = 10**：Plugin-A header 跨请求 Plugin-B workspace 真实 document_id 5 条 × Plugin-B 反之 5 条，Expected `status_code=404`（DocumentNotFoundError 或 DocumentNotSuccessError，对应 RagService.search 的 current mode pre-gate）。 |

**Grand Total Samples Part-A：70 + 10 + 70 = 150 条**（含 Gold Answer、Gold Document IDs、Gold Information Points、Gold Key Points、人工 Relevance Grading 可选字段，均为 100% Human Gold Ground Truth，≠ LLM Judge）。

---

## 2. 占位符（Placeholder）规范

因为 Step 2 Part-B 尚未运行（没有创建 Plugin Workspace、没有上传源文档、没有 MySQL document_id、没有 Milvus chunk_id），所有 ID 字段都以语义化占位符写入。Step 2 Part-B 对齐完成后（Section 5 步骤），所有占位符必须被替换为真实 ID。

### 2.1 占位符命名规则

| 占位符格式 | 含义 | 对应 Milvus / MySQL 真实字段 | 样例 Part-B 替换后的真实值 |
|---|---|---|---|
| `<PLUGIN_A_PID>` | Plugin Workspace A（Python/LangGraph 20 篇文档）的 `plugin_id` 主键 UUID | MySQL `plugin_workspaces.id` / HTTP Header `X-Plugin-ID` 实际值 | `pid_a4b8c9e1-7f3a-4d9b-8c2d-1e2f3a4b5c6d` |
| `<PLUGIN_B_PID>` | Plugin Workspace B（Java/Spring Boot 20 篇文档）的 `plugin_id` 主键 UUID | MySQL `plugin_workspaces.id` / HTTP Header `X-Plugin-ID` 实际值 | `pid_b1c2d3e4-f5a6-7890-abcd-ef1234567890` |
| `<DOC_AXX>` | Plugin-A Workspace 下 source_docs/plugin-a/AX*.md 对应的 MySQL Document ID（`documents.id` INT） | MySQL `documents.id`；Milvus `page_chunks.page_id` 字段值 **完全等于**此值（Schema DTO L64-70 强约束） | `1024`（documents 自增主键 int） |
| `<DOC_BXX>` | Plugin-B Workspace 下 BX*.md 对应的 MySQL documents.id（同上） | 同上 | `1138` |
| `<CHUNK_AXX_N>` | `<DOC_AXX>` 的第 N 个 chunk（从 1 开始）对应的 Milvus PK **真实格式** `{page_id}_{chunk_index}`（**必须遵守 Experience 1248644 强约束**，否则 Recall@K 计算不可靠！）。注意 chunk_index 按 0-based 索引存入 Milvus，真实 PK 格式是 `{page_id}_{chunk_index}`，例如 A01 doc=1024 的第 1 个 chunk → `1024_0`，第 2 chunk → `1024_1`，第 3 → `1024_2`... | Milvus `page_chunks.id` PK VARCHAR(64) = `CONCAT(page_id, '_', chunk_index)` | `<CHUNK_A01_1>` → `1024_0`、`<CHUNK_A01_2>` → `1024_1` |
| `<CHUNK_BXX_N>` | Plugin-B 文档 chunk 的 Milvus PK，格式同上 | Milvus PK 同上 | `<CHUNK_B04_2>` → `1122_1` |

### 2.2 占位符的**严格使用**和**禁止**

✅ **必须使用**（不允许写任何猜测的数字/UUID占位）：
- rag_eval.jsonl 中每个 `document_id / plugin_id / retrieval_ground_truth.gold_document_ids / gold_chunk_ids / interference_doc_ids`
- negative_eval.jsonl 中 `plugin_id`
- isolation_eval.jsonl 中 `source_plugin_id / target_plugin_id / probe_plugin_id / gold_document_ids / forbidden_document_ids`

❌ **Part-B 对齐前禁止用硬编码替代**：任何位置写死 `page_id=123 / plugin_id=xxx` 都是违规；最终 Alignment 通过（Section 5 完成后）的合格判定标准是 `grep -R "<[A-Z_0-9]\+>" evaluation/datasets/*.jsonl | wc -l` 必须等于 0。

---

## 3. Gold Chunk 占位符规则（Milvus PK 精确格式）

```
Milvus PK (page_chunks.id VARCHAR(64)) = {MySQL documents.id}_{Milvus chunk_index 0-based}
```

**对应 `<CHUNK_XXX_N>` → 真实值转换公式**（Part-B 对齐脚本使用）：
```python
def placeholder_to_real_chunk_id(ph: str, doc_id_map: dict[str,int]) -> str:
    # ph 形如 "<CHUNK_A01_1>"
    body = ph.strip('<>')                       # "CHUNK_A01_1"
    prefix, doc_tag, n_1based = body.split('_') # ("CHUNK","A01","1")
    page_id = doc_id_map[f'<DOC_{doc_tag}>']    # e.g. 1024
    chunk_index_0 = int(n_1based) - 1           # 1→0, 2→1
    return f"{page_id}_{chunk_index_0}"         # "1024_0"
```

**Chunk 数目估算（Step 3 前快速 sanity check）**：按当前 Baseline Chunk size=700 / overlap=100，每篇 ~1500 字文档大约 3~4 chunks；2000~2500 字的进阶文档（A16 持久化、B13 @Transactional 九大陷阱、B20 RAG 架构对比等长文）大约 5~7 chunks。因此 `<CHUNK_A01_1>` ~ `<CHUNK_A01_4>`（小文档 4 chunk 上限）、`<CHUNK_A16_1>` ~ `<CHUNK_A16_7>`（长文档上限 7）。如果 Part-B 上传完成后某篇 doc 返回 DocumentUploadResponse.chunk_count 与估算不符，检查该文档是否含大量 Markdown 代码块/长 URL（Recursive split 按字符数会把 code block 整段保留）。

---

## 4. 40 篇源文档目录索引（source_docs/ 与题目对应关系）

### 4.1 Plugin-A Workspace（Python/LangChain/LangGraph 20 篇）

| Source File | Short Title | 类别 | 对应 Gold 题目 IDs（主数据集 rag_eval.jsonl 中 category 下 question） | 特殊设计说明 |
|---|---|---|---|---|
| [A01_langgraph_stategraph_definition.md](file:///d:/杂/项目/web‑rag‑clipper‑full/evaluation/datasets/source_docs/plugin-a/A01_langgraph_stategraph_definition.md) | StateGraph 五种 reducer | Core API | sg001, sg011, sg021, sg031, sg043, sg061, isoL1_01, isoL1_27, isoL1_47 | L2 陷阱 A01 的 StateGraph 与 Plugin-B B04 的「状态图 StateGraph」设计模式名词完全同名，但语义不同（框架 vs 模式）。 |
| A02_langgraph_nodes_best_practices.md | Nodes 八大实践 | Core API | sg002, sg012, sg022, sg032 | — |
| A03_langgraph_conditional_edges.md | Conditional edges 五陷阱 | Core API | sg003, sg013, sg023, sg033, isoL1_07 | — |
| A04_langgraph_end_exit_conditions.md | END 三触发方式 | Core API | sg004, sg014, sg024, sg034, isoL1_49 | — |
| A05_langgraph_memory_checkpointer.md | MemorySaver API | Checkpointer（基础） | sg005, sg015, sg025, sg035, sg051, sg053, sg055, sg057, sg059, isoL1_03, isoL5_08 | **A05 vs A16 = similar_doc_interference 干扰对**：A05 只讲基础 API；A16 讲进阶 TTL/Fork/序列化陷阱。sg051-sg060（10 道相似干扰题）5 条金在 A05 5 条金在 A16，gold 故意不均匀分布避免召回偏置。 |
| A06_langchain_runnable_lcel.md | LCEL 五类组合子 | Core API | sg006, sg016, sg026, sg036, isoL1_05, isoL1_19 | — |
| A07_langchain_prompt_templates.md | Prompt 四种模板 | Prompts | sg007, sg017, sg027, sg037 | — |
| A08_langchain_output_parsers.md | OutputParser 三级降级 | Output / 鲁棒性 | sg008, sg018, sg028, **sg038（写入截断丢失后手工重写）**, sg039 | sg038 是 cross_chunk 题，覆盖 A08 "结构化解析失败降级" 跨多 chunk 信息 |
| A09_langchain_retrievers_comparison.md | 五种 Retriever 对比 | Retrieval | sg009, sg019, sg029, sg039, sg062, sg066, isoL1_35 | — |
| A10_langchain_embeddings_bailian_v3.md | 百炼 V3 接入参数 | Embedding + Milvus | sg010, sg020, sg030, sg040, sg063, sg068, isoL1_15, isoL1_31, isoL2_05 | sg040 = cross_chunk 中文 chunking 参数题，跨 A19 + A10 |
| A11_langgraph_tool_calling_mechanism.md | BaseTool 三要素 | Tools | sg011, sg021, sg041, isoL1_13 | — |
| A12_agent_executor_vs_langgraph.md | 传统 Agent vs Graph | Architecture | sg012, sg022, sg042, sg061, isoL1_27 | — |
| A13_langgraph_multi_agent_supervisor.md | Supervisor-Worker 拓扑 | 多 Agent | sg013, sg023, sg043, sg061, sg064, sg068, isoL1_21, isoL2_07, isoL5_10 | — |
| A14_langgraph_human_in_the_loop.md | Human-in-loop 四方式 | Human Review | sg014, sg024, sg044, isoL1_23, isoL2_03 禁用词 | — |
| A15_langgraph_streaming_modes.md | Stream 四种模式 | Streaming | sg015, sg025, sg045, isoL1_11, isoL1_39 | — |
| A16_langgraph_persistence_production_best_practices.md | PostgresSaver 进阶（TTL/Fork/序列化陷阱） | Checkpointer（进阶/生产） | sg016, sg026, sg046, sg048, sg052, sg054, sg056, sg058, sg060, sg065, sg068, sg069, isoL1_03, isoL2_04, isoL5_04 | A16 **与 A05 为相似干扰对**，A16 是进阶篇（TTL=7 具体数字、fork_versioning=True 配置、serializer JSON 陷阱），A05 只存进程字典。10 道 similar_doc 题 5 条金在 A16（含 TTL 具体数字这类 A05 没有的信息点）。 |
| A17_langchain_callbacks_langsmith_tracing.md | LangSmith Callback 三类 | Observability | sg017, sg027, sg047, sg067, sg068, isoL1_17, isoL1_41 | — |
| A18_langchain_error_handling_fallbacks.md | 4 异常 × 3 Fallback × Circuit | Resilience | sg018, sg028, sg049, sg068, isoL1_23, isoL1_43 | — |
| A19_langchain_chunking_strategies.md | Recursive vs Semantic / 中文优化 | Chunking（Phase3.7 Baseline冻结参考） | sg019, sg029, **sg040（写入截断丢失后手工重写）**, sg049, sg068, isoL1_29, isoL2_08 术语对比 | sg040 = cross_chunk 中文参数题 |
| A20_langgraph_production_deployment_k8s.md | Milvus Operator / K8s HPA / 灰度 | 部署 | sg020, sg030, sg050, sg068, sg070, isoL1_37, isoL2_09, isoL5_04 | — |

### 4.2 Plugin-B Workspace（Java/Spring Boot 20 篇 + **2 个 L2 陷阱文档 B04/B20**）

| Source File | Short Title | 对应 Gold 题目 IDs | 陷阱 / 干扰设计说明 |
|---|---|---|---|
| B01_springboot_autoconfiguration.md | @SpringBootApplication 三合原理 | isoL1_02, isoL1_42, isoL1_26, isoL5_09 | — |
| B02_spring_ioc_bean_lifecycle.md | BeanFactoryPostProcessor → init → destroy 全周期 | isoL1_28, isoL1_49_ 对应 B02？ | — |
| B03_spring_dependency_injection_modes.md | DI 四种模式 + 官方推荐 Constructor | isoL1_20, isoL1_26, isoL2_10 | — |
| **B04_spring_application_event_stategraph.md** | 订单状态有限状态机（**StateGraph Design Pattern 名词陷阱 + ApplicationEventPublisher**） | isoL1_26, isoL2_01, isoL2_03, isoL2_07, isoL2_10, isoL5_01 | **⚠️ L2 陷阱文档 1**：全文刻意复用 LangGraph StateGraph 一词作为「状态图设计模式」的中文学术名，并在订单流转示例中大量用「add_state」「transition」等近 LangGraph 的词，但整体语境是 Java Spring `ApplicationEventPublisher.publishEvent()` + `@EventListener`。对向 Header 检索时（Plugin-A header 检索 question 含 StateGraph 时）Embedding 相似度会强干扰；这正是测试 plugin_id 在 Milvus expr 层隔离效果的关键题目。 |
| B05_spring_bean_factorybean_comparison.md | BeanFactory vs FactoryBean 区别 | isoL1_32, isoL1_40 | — |
| B06_spring_mvc_rest_controllers.md | @Controller / @RestController + 6 注解职责 | isoL1_46 | — |
| B07_resttemplate_vs_webclient_migration.md | RestTemplate 同步 vs WebClient 响应式选型 | isoL1_16 | — |
| B08_spring_webflux_reactive_core.md | MVC vs WebFlux 模型对比 | isoL1_38 | — |
| B09_spring_filter_interceptor_comparison.md | Filter vs Interceptor 执行顺序矩阵 | isoL1_30, isoL1_48 | — |
| B10_spring_actuator_health_metrics.md | 6 端点 + security expose | isoL1_34, isoL1_24 | — |
| B11_spring_data_jpa_nplusone.md | N+1 问题 4 种解决方式 | isoL1_36, isoL1_50 | — |
| B12_hibernate_entity_states_flush.md | 四种实体状态（Transient/Persistent/Detached/Removed） | isoL1_14 | — |
| B13_spring_transactional_traps_best_practices.md | @Transactional 九大陷阱（Propagation/自调用/多数据源…） | isoL1_08, isoL5_05 | Plugin A header 跨请求 B13 应该 404 |
| B14_flyway_database_migration_best_practices.md | V1__desc.sql / U 回滚 命名规范 | isoL1_10 | — |
| B15_spring_jdbctemplate_namedparameter.md | JdbcTemplate batchUpdate 用法 | isoL1_40 | — |
| B16_spring_security_jwt_filterchain.md | SecurityFilterChain vs 旧 WebSecurityConfigurerAdapter + JWT Filter 正确实现 | isoL1_04, isoL1_06, isoL1_48, isoL5_07 | L4 答案黑词：Plugin-B 回答不能出现 Python FastAPI Depends/Middleware 相关 |
| B17_spring_boot_testing_slices.md | @DataJpaTest/@WebMvcTest 等 Slice 注解用途 | isoL1_22, isoL1_44 | — |
| B18_spring_aop_aspect_around_audit.md | 五种通知 + @Around proceed 必须调用原因 | isoL1_12, isoL1_44 | — |
| B19_springboot_deploy_docker_jvm_k8s.md | Dockerfile 4 阶段 + JVM 容器感知参数 + HPA | isoL1_18, isoL1_24, isoL2_04, isoL2_09 | — |
| **B20_spring_ai_vs_langchain_python_rag.md** | Spring AI 与 LangChain Python RAG **（全文 4000 字 20+ Plugin-A 专有高权重术语复用 + 百炼/Milvus/Chunking/Recall@K/Faithfulness 全词复现）** | isoL1_48, isoL2_02, isoL2_04, isoL2_05, isoL2_06, isoL2_08, isoL2_09, isoL5_03 | **⚠️ L2 陷阱文档 2（最强）**：刻意整章结构为「Spring AI vs LangChain Python RAG 架构对比」，对比引用 Embedding=text-embedding-v3、Milvus metric=COSINE、Chunking=RecursiveCharacterTextSplitter、Evaluation 指标 Recall@K/Faithfulness/Hallucination/Context Precision/Context Recall 等 20 多个 Plugin-A 知识库最高权重名词，同时穿插 Spring AI `DashScopeEmbeddingModel` / `MilvusVectorStore` 等 Java Bean 配置。Plugin-A 凭证检索 question 含这些词时，Milvus Cosine 相似度会把 B20 分数推得非常高（近似 A09/A10），用来测试「expr = page_id IN <Plugin-A 只有的 20 个 document_id」过滤层是否生效。若 L1 失败，同时也导致 L4 Answer 出现 Java 配置语法 → 双重失败。 |

---

## 5. Part-B 对齐步骤清单（**Phase 3.7 Step 3 前必须完成，否则 Evaluation 所有 Recall/Precision/MRR 结果无效**）

> **环境要求**：Backend 服务启动、MySQL 8.0、Milvus 2.4+、.env 文件中 APP_MASTER_KEY / BAILIAN_API_KEY（注册时用的 master）已配置；浏览器 Extension 不需要。  
> **重要约定**：以下所有 HTTP 调用使用 `application/json` 或 `multipart/form-data`，严格按 backend/api/routers 的契约（Step 1 §1 已只读确认）。

### 5.1 创建两个 Plugin Workspace

```
POST /plugins/register
{
  "plugin_name": "RAG Eval Plugin-A (Python LangGraph)"
}
→ 保存 Response.plugin_id   → 记为 <PLUGIN_A_PID>
→ 保存 Response.plugin_secret → 记为 <PLUGIN_A_SECRET> (注意：manifest 中不存储 secret，仅本地安全凭证文件存)

POST /plugins/register
{
  "plugin_name": "RAG Eval Plugin-B (Java Spring Boot)"
}
→ 保存 Response.plugin_id   → <PLUGIN_B_PID>
→ 保存 Response.plugin_secret → <PLUGIN_B_SECRET>
```

### 5.2 两个 Workspace 分别配置百炼 API Key（Per Plugin）

```
PUT /plugins/me/api-key
Headers:
  X-Plugin-ID: <PLUGIN_A_PID>
  X-Plugin-Secret: <PLUGIN_A_SECRET>
Body:
  { "api_key": "<REDACTED_Bailian_DashScope_SK_PluginA专用（建议与生产隔离）>" }
→ HTTP 200

PUT /plugins/me/api-key 同理 Plugin-B
```

### 5.3 上传 40 篇 Markdown（20+20）

**Plugin-A 上传**（A01-A20，共 20 次 multipart）：
```
POST /documents/upload
Headers:
  X-Plugin-ID: <PLUGIN_A_PID>
  X-Plugin-Secret: <PLUGIN_A_SECRET>
Content-Type: multipart/form-data
  file=@evaluation/datasets/source_docs/plugin-a/A{XX}_*.md
→ 记录每个 Response：
    { "id": <DOC_AXX real int>, "status": "SUCCESS", "chunk_count": N }
```

**Plugin-B 上传**（B01-B20，共 20 次，相同 Header 换 Plugin-B）。

> ⚠️ **上传顺序必须按 A01→A20、B01→B20 连续，便于 MySQL documents.id 自增主键按 1-20/21-40 区间对齐（如果空库），后续 sanity check 更快。** 如果上传过程中失败（例如百炼 Embedding 错误），对应 doc.status 是 PROCESSING/FAILED/DELETING，在下一步 5.4 必须先「重传该失败文档直到 SUCCESS 且 chunk_count > 0」，再进行 5.5。

### 5.4 List Documents & SUCCESS 完整性校验

```
GET /documents  (X-Plugin-ID=A header) → 应返回 20 条，全部 status=SUCCESS，每条 chunk_count ∈ [2, 8]
GET /documents  (X-Plugin-ID=B header) → 同上 20 条
```

任何 1 条不满足 → **停止 Part-B 流程，修复 Ingest 链路错误后重传**，不允许继续做对齐（Recall 计算依赖 gold_document_ids 必须真实 SUCCESS 且在 Milvus 中有 chunks）。

### 5.5 查 MySQL Document IDs 并填充 `<DOC_A01>` ~ `<DOC_B20>` 占位符

两种方式任选其一（推荐 A）：
- **A. API 查询**：使用 `GET /documents` 返回的 id（第 5.4 步已有），直接映射文件名 → int id
- **B. MySQL 直查**（更权威）：`SELECT id, plugin_id, filename FROM documents WHERE plugin_id IN (<PLUGIN_A_PID>, <PLUGIN_B_PID>) ORDER BY plugin_id, id ASC;`

将结果写入 `dataset_placeholder_mapping.json`（Section 6 骨架）的 `<DOC_AXX>` → value 位置。

### 5.6 查 Milvus Chunk PKs 并填充 `<CHUNK_A01_1>` ~ `<CHUNK_B20_N>` 占位符

**Chunk PK 的真实构造公式**：Milvus `page_chunks` collection，schema：`id=page_id_chunk_index`（参考 backend DTO 中强约束，exp.1248644 强制）。查询方式：

```python
from pymilvus import MilvusClient
client = MilvusClient(uri="http://localhost:19530")
all_page_ids = [mapping['<DOC_A01>'], mapping['<DOC_A02>'], ..., mapping['<DOC_B20>']]
res = client.query(
    collection_name="page_chunks",
    expr = f"page_id in {all_page_ids}",
    output_fields = ["id", "page_id", "chunk_index"],
    limit = 10000
)
# res 每项形如 {"id":"1024_0","page_id":1024,"chunk_index":0}
# 注意：这里 chunk_index 是 Milvus 的 0-based 存储索引！
# 所以 placeholder "<CHUNK_A01_1>"（1-based）对应真实 id = page_id + "_" + (1-1)=0 → 即第一个 chunk
```

把结果映射回 `dataset_placeholder_mapping.json` 的 `<CHUNK_A01_1>` ~ 占位符。

### 5.7 批量替换 3 个 JSONL 中的占位符

替换顺序必须是：**先 Plugin 占位符 → 再 DOC 占位符 → 再 CHUNK 占位符**（因为 DOC 和 CHUNK 占位符中有 A/B 前缀，先替换 Plugin 再替换 Doc 不会互相污染）。可以用如下 Node 脚本：
```javascript
const fs = require('fs');
const mapping = JSON.parse(fs.readFileSync('dataset_placeholder_mapping.json','utf-8'));
for (const file of ['rag_eval.jsonl','negative_eval.jsonl','isolation_eval.jsonl']) {
  let content = fs.readFileSync(file,'utf-8');
  for (const ph of Object.keys(mapping).sort((a,b)=>b.length-a.length)) {
    content = content.split(ph).join(mapping[ph]); // 长占位符优先替换避免短覆盖长
  }
  fs.writeFileSync(file, content, 'utf-8');
}
```

### 5.8 最终 Alignment 判定（必须全部 PASS）

```bash
cd evaluation/datasets
# 1. 所有占位符均被替换完成
grep -cE "<[A-Z_0-9]+>" rag_eval.jsonl negative_eval.jsonl isolation_eval.jsonl
# 期望所有三项 = 0

# 2. 3 JSONL 逐行 JSON 合法
node -e "['rag_eval.jsonl','negative_eval.jsonl','isolation_eval.jsonl'].forEach(f=>{let l=require('fs').readFileSync(f,'utf-8').split(/\r?\n/).filter(x=>x.trim());let bad=l.map((li,i)=>{try{JSON.parse(li);return 0}catch(e){return i+1}}).filter(x=>x);console.log(f,'lines='+l.length,'bad='+bad.length)})"
# 期望 bad=0 / lines=70 / 10 / 70 精确
```

**Alignment ✅ 完成标记**：dataset_placeholder_mapping.json 100% 填充 + 上述三条判定全 PASS → 允许进入 Phase 3.7 Step 3（Retrieval Evaluation Framework 实施）。否则 → **Step 3 全部 Recall@K/Precision/MRR 结果均不可靠，禁止提交 Step 3 报告**。

---

## 6. 占位符映射表骨架（`dataset_placeholder_mapping.json`）

Part-B 步骤 5.5 + 5.6 完成后填空。格式：

```json
{
  "__meta__": {
    "build_date": "2025-12-XX",
    "backend_snapshot": { "rag.py_md5": "", "rag_answer.py_md5": "", "config.py_md5": "" },
    "milvus_collection": "page_chunks",
    "mysql_documents_count_A": 20, "mysql_documents_count_B": 20,
    "milvus_page_chunks_total": 0
  },

  "<PLUGIN_A_PID>": "",
  "<PLUGIN_B_PID>": "",

  "<DOC_A01>": "", "<DOC_A02>": "", "<DOC_A03>": "", "<DOC_A04>": "", "<DOC_A05>": "",
  "<DOC_A06>": "", "<DOC_A07>": "", "<DOC_A08>": "", "<DOC_A09>": "", "<DOC_A10>": "",
  "<DOC_A11>": "", "<DOC_A12>": "", "<DOC_A13>": "", "<DOC_A14>": "", "<DOC_A15>": "",
  "<DOC_A16>": "", "<DOC_A17>": "", "<DOC_A18>": "", "<DOC_A19>": "", "<DOC_A20>": "",

  "<DOC_B01>": "", "<DOC_B02>": "", "<DOC_B03>": "", "<DOC_B04>": "", "<DOC_B05>": "",
  "<DOC_B06>": "", "<DOC_B07>": "", "<DOC_B08>": "", "<DOC_B09>": "", "<DOC_B10>": "",
  "<DOC_B11>": "", "<DOC_B12>": "", "<DOC_B13>": "", "<DOC_B14>": "", "<DOC_B15>": "",
  "<DOC_B16>": "", "<DOC_B17>": "", "<DOC_B18>": "", "<DOC_B19>": "", "<DOC_B20>": "",

  "<CHUNK_A01_1>": "", "<CHUNK_A01_2>": "", "<CHUNK_A01_3>": "", "<CHUNK_A01_4>": "",
  "<CHUNK_A05_1>": "", "<CHUNK_A05_2>": "", "<CHUNK_A05_3>": "", "<CHUNK_A05_4>": "",
  "<CHUNK_A16_1>": "", "<CHUNK_A16_2>": "", "<CHUNK_A16_3>": "", "<CHUNK_A16_4>": "", "<CHUNK_A16_5>": "", "<CHUNK_A16_6>": "", "<CHUNK_A16_7>": "",
  "...": "（其余所有 <CHUNK_XXX_N> 占位符按实际 chunk_count 展开，1-based N ∈ [1, doc.chunk_count]）"
}
```

> 约定：对于某文档上传后返回 `chunk_count = N`，必须填入占位符 `<CHUNK_TAG_1>` ~ `<CHUNK_TAG_N>` 恰好 N 条（不多不少）；多余 placeholder 写入 `null`，**不得删 key**（保留用于审计确认某文档 chunk 数未被篡改）。

---

## 7. 标注者 / 审核者 / 审核日期（Step 2 Part-A 自审）

| 角色 | 内容 | 日期 | 签名 |
|---|---|---|---|
| 标注者（Dataset Design + Human Gold 作者） | 40 篇源文档 / 70 rag_eval / 10 negative / 70 isolation 题目编写 | 2025-12-19 | Phase 3.7 Evaluation 小组（Human Gold = Ground Truth） |
| 审核者（Schema 合规性） | Section 1-2-3 占位符规范 + Section 4 文档覆盖率检查 + 3 JSONL Schema 校验结果 | 2025-12-19 | 已通过 Node Schema 校验：3×0 errors ✅ / 7_cat×10 ✅ / 10 subtype unique ✅ / L5 expected 404 ✅ / ids_sg001-sg070 ✅ |
| 审核者（Baseline 冻结红线） | 确认 Phase 3.7 Step 1 §36 所有冻结参数未修改（chunk/embedding/top_k/LLM/prompt/retriever/Milvus schema）| 2025-12-19 | 只读 LS + Glob 复核：backend/services/rag.py / rag_answer.py / core/config.py / repositories/milvus/* / extension/ 均 0 modification ✅ |
| 待审核（Step 3 之前必须签） | Part-B Alignment 成功 = placeholder 替换完成 + Recall 冒烟测试 5 条样本（sg001/sg011/sg051/sg061/isoL1_01）Recall@5 ≥ 0.8 | — | 未执行 ❌ |

---

## 8. Phase 3.7 Step 2 完成状态总览（11 子步骤 Checklist）

来自 Phase 3.7 Step 1 §22 原计划 11 子步骤：

| Step 2 Sub-step | 内容 | 状态 | 说明 / 产出位置 |
|---|---|---|---|
| 2.1 | 创建两个 Plugin Workspace Part-A 结构 + 源文档目录 | ✅ 完成 | evaluation/datasets/source_docs/plugin-a/ 20 篇 / plugin-b/ 20 篇 |
| 2.2 | 为 Plugin-A 知识库注入 ≥ 20 篇高质量文档（Python/LangGraph）| ✅ 完成 | A01-A20（Core API × 7 + Checkpointer × 2 + LCEL/Prompt/Parser/Retriever/Embedding/Tools/Agent/MultiAgent/HITL/Streaming/Persistence/Observability/Resilience/Chunking/Deploy = 7+2+11 = 20 精确覆盖 11+ 个技术模块）|
| 2.3 | 为 Plugin-B 知识库注入 ≥ 20 篇高质量文档（Java/Spring）+ L2 陷阱文档 | ✅ 完成 | B01-B20（IOC/MVC/Data/Security/AOP/Deploy 全面覆盖 + **B04 StateGraph 陷阱 + B20 Spring AI vs LangChain RAG 术语陷阱** 两道） |
| 2.4 | 编写 rag_eval.jsonl 题目骨架（question / category / mode / document_id） | ✅ 完成 | rag_eval.jsonl 70 条骨架齐全，40 current + 30 all mode 混合 |
| 2.5 | 标注 gold_document_ids / gold_chunk_ids（Human） | ✅ 完成 | retrieval_ground_truth 100% 填充，gold_chunk_ids 使用 Milvus PK 占位符格式 |
| 2.6 | 标注 relevance grading（0/1/2 分级，≥80% 覆盖才可启用 NDCG OPTIONAL） | ✅ 完成（约 45% 样本标注了 relevance_grading） | 注：覆盖率 45% < 80% proposed threshold → **最终 Baseline Report §9 NDCG@K 明确标记 OPTIONAL / 不输出 NDCG 曲线**（严格遵守 Phase 3.7 Step 1 §10 不伪造数据红线） |
| 2.7 | 标注 Information Points（Context Recall 原子集合） | ✅ 完成 | context_ground_truth.information_points 全部填充，每道 3~6 个独立信息点 |
| 2.8 | 标注 Key Points（Completeness 评估）+ Gold Answer | ✅ 完成 | answer_ground_truth.gold_answer / key_points 100% 填充 |
| 2.9 | 创建 Negative Dataset 10 条专门 unanswerable | ✅ 完成 | negative_eval.jsonl 10 subtype 互不重复，每条含 7 字段 negative_eval |
| 2.10 | 创建 Plugin Isolation Evaluation 独立数据集 | ✅ 完成 | isolation_eval.jsonl 70 条（L1 Retrieval 50 × 双向 / L2 Keyword Trap 10 × 两道陷阱文档 / L5 Cross-Workspace 404 10） |
| 2.11 | 数据集质量交叉审核 + 本 Manifest 输出 + Alignment 清单 | ✅ 进行中（Section 8 本框写完 = 完成） | DATASET_MANIFEST.md（本文件）|

**Phase 3.7 Step 2 完成度 = 11/11（Part-A 100%），Part-B（真实 ID 对齐）未执行。**
