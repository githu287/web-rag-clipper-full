# LangGraph 多 Agent 协作（Supervisor + Worker）模式

当单个 Agent 的 Prompt 和工具列表变得过大时，必须把任务拆解为多 Agent 协作。本文介绍最成熟的 Supervisor-Worker 模式在 LangGraph 中的实现。

## 一、拓扑结构

```
Supervisor (LLM 路由)
    ├─ ResearchAgent  — 工具: web_search, arxiv, wikipedia
    ├─ CodeAgent      — 工具: python_repl, git, file_system
    └─ WriteAgent     — 工具: doc_template, grammar_check
```

Supervisor 的职责是：接收用户问题、决定下一个激活的 Worker、收集 Worker 输出、判断是否需要再路由给其他 Worker，或者结束。

## 二、Supervisor 的 State 设计

```python
class MultiAgentState(TypedDict):
    messages: Annotated[list[BaseMessage], operator.add]
    current_worker: str
    worker_result: dict[str, str]  # worker_name -> 摘要结果
    next_action: Literal["route", "end"]
```

worker_result 字段非常关键。避免把每个 Worker 的完整 messages 列表（可能非常长）不断累加到全局 state，造成上下文爆炸。正确做法是让每个 Worker 只把输出总结成一段 500 字符以内的摘要写入 worker_result，Supervisor 基于这些摘要做下一轮路由决策。

## 三、Supervisor 路由节点实现

```python
def supervisor_node(state):
    prompt = (
        "你是 Supervisor。以下是各 Worker 的最新结果：{worker_result}\n"
        "用户原始需求：{question}\n"
        "决定下一个调用哪个 Worker（research/code/write），或返回 end 表示结束。"
    )
    response = llm.invoke(prompt.format(...))
    parsed = route_parser.parse(response.content)  # {next: str}
    return {"next_action": "end" if parsed.next == "end" else "route",
            "current_worker": parsed.next}
```

## 四、Worker 节点包装

每个 Worker 应该是一个独立的 CompiledGraph 或 Runnable。Supervisor 调用时把它当作黑盒：

```python
def research_worker_node(state):
    worker_input = state["messages"][-1].content + "\n[已收集结果]\n" + summarize(state["worker_result"])
    result = research_graph.invoke({"messages": [HumanMessage(content=worker_input)]})
    return {"worker_result": {"research": extract_summary(result)},
            "messages": [ToolMessage(name="research_worker", content=extract_summary(result), tool_call_id="")]}
```

注意 worker_result 只写 summary，不把 10KB 的 messages 全部追加。

## 五、常见陷阱

1. 上下文爆炸 → 严格限制 messages 累积，用 worker_result 摘要替代全文。
2. 路由死循环 → Supervisor Prompt 强制加「同一 Worker 最多调用 3 次」规则。
3. 结果重复 → worker_result 用 dict key 覆盖而非追加。
4. 依赖顺序丢失 → 在 State 中增加 completed_steps 列表，Supervisor Prompt 参考。
5. 路由 Prompt 太长 → 用 Pydantic 结构化输出 next、reason 两字段，减少废话。

## 六、变体：层级式协作

3 个以上 Worker 时，可再引入一层 MidSupervisor：TopSupervisor → (ResearchSupervisor, EngineeringSupervisor) → Workers。LangGraph 天然支持嵌套 CompiledGraph。
