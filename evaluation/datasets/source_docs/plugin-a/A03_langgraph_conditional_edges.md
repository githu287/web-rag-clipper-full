# LangGraph 条件边（Conditional Edges）完全指南

条件边是 LangGraph 区别于普通线性 Pipeline 的关键抽象。它允许运行时根据当前状态动态选择下一个节点，从而实现 Agent 常见的循环、分支、早停等控制流。

## 一、add_conditional_edges 完整签名

方法签名为：

```python
workflow.add_conditional_edges(
    source: str,
    condition: Callable[[State], str],
    edge_mapping: dict[str, str] | None = None,
)
```

source 是起始节点名。condition 是一个接受状态返回字符串键的纯函数（不允许修改 state，只能读）。edge_mapping 将字符串键映射到目标节点名，或者 END 常量。若省略 mapping，则默认 condition 返回值本身就是目标节点名。

## 二、两种主流模式

模式一：工具调用循环。这是最经典的用途。

```python
def route_after_model(state: AgentState) -> str:
    last_msg = state["messages"][-1]
    if last_msg.tool_calls:
        return "call_tools"
    return END
```

call_model 节点执行完毕后走条件边：若 LLM 请求工具 → call_tools → 普通边回到 call_model；否则 → END。这形成了 ReAct 的 Observe-Thought-Action 闭环。

模式二：多阶段路由。条件边把问题分为"检索型"、"总结型"、"拒绝型"三个分支：

```python
def classifier_router(state):
    intent = classify(state["messages"][-1].content)
    mapping = {"retrieval": "rag_node", "summary": "summarize_node", "off_topic": "reject_node"}
    return mapping.get(intent, END)
```

## 三、十大常见陷阱

1. condition 函数意外修改了 state 字典 → 应转换为只读，使用 copy.deepcopy 后再操作。
2. mapping 字典缺失某个返回键 → 显式 `.get(key, END)` 兜底，防止 GraphValidationError。
3. 条件分支形成无法到达 END 的死循环 → 必须加最大步数限制 `config=RunnableConfig(recursion_limit=25)`。
4. 用 `==` 对 AIMessage.content 判断空串而忽略 tool_calls → 判断条件必须先看 tool_calls。
5. END 常量与字符串 "end" 混用 → 必须使用 graph.END。
6. condition 返回包含大小写的字符串，但 mapping 使用小写 → 统一规范化。
7. 同一 source 节点挂两条条件边 → 行为未定义，必须合并为一个 condition。
8. 条件函数内直接调用 LLM（非纯函数）→ 改造成独立节点，condition 只读取该节点输出的 next_step 字段。
9. 返回类型为 int/tuple 而非 str → 必须强转字符串。
10. 多个分支最终合并回同一节点但忘记加普通边收尾 → 导致后续节点不可达。

## 四、条件边的可观测性

可以在 condition 函数中 `print(f"[Router] source={source} -> {result}")` 进行打点。更高级的方式是将路由结果写入 state 的 `debug_trace` 列表字段，最终返回给前端调试面板。
