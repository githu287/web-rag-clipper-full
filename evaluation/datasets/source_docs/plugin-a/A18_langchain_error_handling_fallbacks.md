# LangChain 错误处理与降级机制设计

LLM 应用天然依赖外部 API（Embedding、LLM、Milvus、工具调用），任何一步都可能失败。好的错误处理能把 API 级失败转化为用户可理解的行为。

## 一、异常分类

建议把所有异常分为四类：

1. **Transient**（瞬时失败）：429 限流、502、503、504、网络超时、连接重置 → 可重试。
2. **InputValidation**（输入不合法）：空 query、超长 query、chunk 超 embedding 窗口 → 立刻返回 4xx 级提示用户。
3. **ContentFilter**（合规拒绝）：LLM 安全过滤命中 → 统一返回「该问题涉及敏感内容，请修改措辞」。
4. **Systemic**（系统级故障）：Milvus 失联、数据库连不上、磁盘满、APP_MASTER_KEY 错误 → 立刻告警 + 降级响应。

## 二、Runnable 的重试与降级

使用 `.with_retry()` 和 `.with_fallbacks()`：

```python
llm_primary = ChatOpenAI(model="qwen-plus", temperature=0)
llm_fallback = ChatOpenAI(model="qwen-turbo", temperature=0)

robust_llm = (
    llm_primary
    .with_retry(stop_after_attempt=3, wait=tenacity.wait_exponential_jitter(), retry_if_exception_type=(429, 503))
    .with_fallbacks([llm_fallback])
)
```

fallback 的触发条件是主模型抛出 Exception 组，可通过 `exceptions_to_handle=(...)` 指定。fallback 链本身也可以再接 .with_retry()。

## 三、RAG 场景的三级降级

RAG 有三级质量降级，依次触发：

- **L1 正常**：Embedding → Milvus(k=10) → Context 4000 字符 → LLM。
- **L2 召回为空降级**：Milvus 返回 0 条或最高 score < 阈值（COSINE 0.3）。此时不调用 LLM，直接返回「当前知识库中没有足够信息回答该问题」。
- **L3 LLM 失败降级**：主模型 5 次重试全部失败 + fallback 模型也失败 → 返回「AI 服务暂时繁忙，请稍后重试」并在后台生成告警 Ticket。

## 四、Embedding 失败的特殊处理

Embedding 失败是 RAG 独有的致命失败：没有向量就无法检索。必须区分：
- embed_query 失败（单条）→ 走 L3。
- embed_documents 失败（批量 ingest 时）→ 对失败 batch 单独重跑 3 次；全部失败后记录为 Document FAILED + error_message = Embedding batch failed，供后续手动 re-ingest。

## 五、工具调用错误处理

每个 BaseTool._run 外层应该 try/except 捕获所有异常，把异常类型、消息、traceback 摘要包装成 ToolMessage 返回（而不是抛出）。这样下一轮 LLM 能看到失败信息并修正参数。典型模式：

```python
def _run(self, ...):
    try:
        return do_work(...)
    except Exception as e:
        return f"[ToolError: {type(e).__name__}] {str(e)[:500]}"
```

## 六、异常埋点

所有异常在最终 handler 层统一打点：status_code、exception_type、tenant_id、endpoint、elapsed_ms 六个字段输出到 Prometheus Histogram。告警规则：Systemic 级异常 >1 次/5 分钟 → Pager。
