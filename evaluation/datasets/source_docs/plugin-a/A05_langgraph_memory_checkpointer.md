# LangGraph Memory 与 Checkpointer 持久化方案

LangGraph 提供两类状态保留机制：运行时 Memory（仅会话内）和 Checkpointer（持久化跨进程）。正确选型对生产系统的可恢复性至关重要。

## 一、MemorySaver（内存级 Checkpointer）

最简单的 Checkpointer 实现是 MemorySaver，它把每个 thread_id 的所有 checkpoint 存于 Python 进程内存字典中。典型用法：

```python
from langgraph.checkpoint.memory import MemorySaver
memory = MemorySaver()
graph = workflow.compile(checkpointer=memory)
config = {"configurable": {"thread_id": "session-123"}}
result = graph.invoke({"messages": [...]}, config=config)
```

特点：零依赖、快、适合开发和单实例部署；缺点是进程重启即丢失，不支持多实例横向扩展。

## 二、SqliteSaver 和 PostgresSaver

生产环境推荐使用 SqliteSaver 或 PostgresSaver。它们把 checkpoint 序列化写入关系数据库的特定表中，由 thread_id 和 checkpoint_id 双重索引。

```python
from langgraph.checkpoint.sqlite import SqliteSaver
import sqlite3
conn = sqlite3.connect("checkpoints.db")
checkpointer = SqliteSaver(conn)
```

支持的关键能力：(1) 跨进程状态恢复；(2) fork 出多分支并行推演（同 thread_id 不同 checkpoint_ns）；(3) 时间旅行回到指定 checkpoint 重放；(4) TTL 自动清理旧 checkpoint。

## 三、与 StateGraph 字段的交互

当使用 Checkpointer 时，CompiledGraph 会在每个节点执行完成后自动把当前完整 state 快照序列化并写入 checkpointer。必须注意 State Schema 中所有字段必须是 JSON 可序列化的。无法被 pickle/json 序列化的字段（如 open socket、file handle）会导致 checkpoint 写失败。

## 四、Interrupt 与人工审核（Human-in-the-loop）

LangGraph 的 interrupt_before 参数允许在指定节点前暂停执行，把控制权交给外部系统等待人工输入后再 resume。这必须配合 Checkpointer 使用：

```python
graph = workflow.compile(checkpointer=memory, interrupt_before=["human_review"])
# 第一次调用执行到 human_review 前暂停
snapshot = graph.invoke(input, config)
# 人工审核后注入继续指令
graph.update_state(config, {"messages": [HumanMessage(content="Approved")]})
final = graph.invoke(None, config)
```

## 五、陷阱清单

1. 多个 Graph 实例共享同一个 MemorySaver 对象时发生 thread_id 冲突 → 每个 CompiledGraph 独立使用或对 thread_id 加命名空间前缀。
2. SqliteSaver 在 Windows 上默认 WAL 模式与 antivirus 冲突 → 关闭 WAL 或改用 Postgres。
3. 超大 messages 列表序列化慢 → 只把消息 ID 和摘要写入 checkpoint，真实消息走对象存储。
4. Checkpointer 的 update_state 被误用修改了 reducer 累加字段 → 必须用 None 触发 resume 而非直接追加 messages。
5. 多实例部署下并发写同一 thread_id 导致 checkpoint 覆盖 → 外部加分布式锁或使用乐观锁 + retry。

## 六、选型建议

本地开发：MemorySaver；单人 Demo：SqliteSaver；生产多实例：PostgresSaver 配合 Redis 缓存热 thread。
