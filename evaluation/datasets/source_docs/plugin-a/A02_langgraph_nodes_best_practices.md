# LangGraph 节点（Node）编写规范与高级技巧

节点是 LangGraph StateGraph 中最小的执行单元。每一个 Node 对应一个 Python 可调用对象。本文从「输入-处理-输出」三个维度系统讲解节点的编写规范，并总结六个高级技巧。

## 一、节点函数的标准签名

节点函数必须遵循统一的签名：

```python
def node_name(state: AgentState) -> dict:
    ...
```

入参是完整的当前状态字典。返回值必须是 dict，且其键必须是 State Schema 的字段子集。如果一个节点无需修改状态（仅产生副作用如日志打点），也必须返回空字典 `{}`，禁止返回 None。

异步节点签名与此对应：

```python
async def node_name(state: AgentState) -> dict:
    ...
```

## 二、四大单一职责节点

标准化的 Agent 通常由以下四种节点组成：

1. `call_model`：构造 messages 并调用 LangChain LLM Runnable，返回 `{"messages": [ai_msg]}`。
2. `should_continue`：检查最近一条 AIMessage 是否包含 tool_calls，返回 `{"next_step": "tools" or "end"}`。
3. `call_tools`：依次执行 tool_calls 并收集 ToolMessage，返回 `{"messages": tool_messages, "tool_result": combined_dict}`。
4. `human_review`：挂起执行（需要 Memory/Interrupt 配合），等待人工输入后返回 `{"messages": [human_msg]}`。

这四种节点足以覆盖 80% 的 ReAct Agent 场景。

## 三、高级技巧

技巧 1：使用 functools.partial 绑定额外参数。当节点需要注入 service 对象而不污染 State 时，用 partial 把 service 绑定为节点的闭包变量。

技巧 2：幂等保护。在节点内部读取 state 中已计算的字段，若存在则直接返回，避免 Streaming 下重入导致的重复工具调用。

技巧 3：异常包装为特殊消息。节点内 try/except 捕获网络错误和工具执行错误，将异常描述作为 SystemMessage 追加到 messages，让下一轮 call_model 能感知失败原因。

技巧 4：返回增量而非全量。配合 Annotated operator.add reducer，只返回本轮新增的 messages 片段，减少每次复制的内存开销。

技巧 5：节点内部校验。对 state.members[-1].content 做空串与超长检查，避免空输入进入 LLM 造成 token 浪费。

技巧 6：结构化元数据输出。在 tool_result 字段专门记录 `tool_name`、`duration_ms`、`success`、`error_code` 四个元数据，便于后续 tracing 和统计。

## 四、节点调试

LangGraph 支持 `graph.get_graph().print_ascii()` 打印拓扑。调试节点行为时，可通过 `graph.stream(input, stream_mode="updates")` 逐节点观察增量输出，与预期的 reducer 合并结果对比。常见问题是 messages 顺序错乱——根因通常是异步并发节点合并时 operator.add 的未定义顺序。
