# LangGraph Human-in-the-loop 审核中断机制

金融、医疗、法律等生产 Agent 场景要求「高风险操作必须经过人工确认」。LangGraph 通过 `interrupt_before` + Checkpointer 实现该模式。

## 一、interrupt_before 的基本用法

compile 时声明 interrupt_before 是一个节点名列表：

```python
graph = workflow.compile(
    checkpointer=memory,
    interrupt_before=["transfer_funds", "send_email"],
)
```

当 Graph 正常推进到即将执行 "transfer_funds" 节点之前，会自动暂停并返回当前状态快照。从外部视角看，就是 graph.invoke() 返回了，但流程并未结束。

## 二、等待人工输入并恢复

暂停后，人工审核界面展示 state 中的关键信息（转账金额、收款人、目的、AI 给出的原因）。审核员选择 Approve / Reject / Modify。系统通过 `graph.update_state()` 注入结果，然后再 invoke(None) 恢复：

```python
# 暂停状态
snapshot = graph.invoke(initial_input, config)
assert graph.get_state(config).next == ("transfer_funds",)  # 预期暂停点

# 人工决策注入
graph.update_state(
    config,
    {"messages": [HumanMessage(content="人工审核通过，允许执行转账。")],
     "approval": {"decision": "approve", "reviewer_id": "u_1024", "time": "..."}}
)

# 继续执行
final = graph.invoke(None, config)
```

## 三、interrupt_after 的互补用法

interrupt_after 表示「某节点执行完立刻暂停」。它适合：call_tools 节点之后立刻暂停，人工检查工具返回结果正确性后再进入下一节点的 LLM 思考。

## 四、State 中必须保留的审计字段

审核系统要求每一步可追溯。推荐 State 中追加以下字段：
- `step_index: int`：已执行步数。
- `approval_chain: list[dict]`：每次 update_state 注入的 decision + reviewer + time + comment。
- `audit_log: list[str]`：各节点自动产出的操作摘要。
- `risk_level: str`：由当前节点类型预评估 risk（low/medium/high），决定是否强制 interrupt。

## 五、陷阱

1. interrupt_before 声明的节点在某一条路径下永远不可达 → 不会报错但审核失效，需加单测覆盖每条路径。
2. 忘记配 Checkpointer → interrupt 不生效。
3. update_state 后重新 invoke 时传入新的 input 而非 None → 会被作为初始输入叠加，污染 messages。
4. 两个审核节点间隔很近 → 每次恢复后需要重新 `get_state(config).next` 判定是否又遇到新中断点，循环处理。
5. Reject 场景未提供后续节点路径 → 直接走条件边到 end，返回完整 audit_log 给用户说明为什么拒绝。
