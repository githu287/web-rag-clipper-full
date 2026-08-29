# LangGraph END 常量与 Graph 退出条件详解

LangGraph 提供特殊的 END 常量用于标记流程终止。正确使用 END 是保证 Agent 不会陷入死循环的关键。本文系统讲解 END 在三种场景下的用法与对应的退出条件设计。

## 一、END 的本质

END 是 `langgraph.graph.END` 模块导出的一个特殊字符串对象（实际值为 `"__end__"`），在 StateGraph 内部被识别为「终止节点」。任何边（普通边或条件边）指向 END 时，CompiledGraph 在执行完当前节点后立即停止调用后续节点，将当前聚合状态作为最终结果返回。

## 二、场景一：线性链的尾部

最简单的用法是普通边串联最后一个节点到 END：

```python
workflow.add_edge("final_summarize", END)
```

这种写法等价于不写（编译时会自动把没有出边的节点默认为到 END），但显式写出来可读性更好。

## 三、场景二：条件分支的正常退出

条件边的 mapping 中必须包含 END 分支。一个常见的三出口模式：

```python
def router(state) -> str:
    if state["next_step"] == "tools":
        return "call_tools"
    elif state["next_step"] == "refine":
        return "refine_node"
    else:
        return END
```

缺少最后 else return END 会导致任何未匹配的 next_step 值抛出 MissingBranchError。推荐规则：所有条件边函数必须覆盖 100% 分支，且默认分支一律为 END。

## 四、场景三：最大步数保护退出

即使业务逻辑没有显式退出，也必须设置 `recursion_limit`。典型写法是：

```python
from langchain_core.runnables import RunnableConfig
config = RunnableConfig(recursion_limit=25)
result = graph.invoke(input_state, config=config)
```

当节点调用总次数超过 recursion_limit 时，LangGraph 会抛出 `GraphRecursionError` 而不是无限循环。推荐默认值 25。生产环境建议记录 recursion_limit hit 次数作为监控指标。

## 五、四种错误的退出条件

错误 1：依靠工具返回的特定字符串 "done" 结束 → 当工具超时或失败时永远不结束。
错误 2：省略 END 分支完全依赖默认分支 → 当代码新增映射项时容易遗漏。
错误 3：把 END 当作节点调用 add_node("__end__", func) → 会产生重复终止节点导致编译错误。
错误 4：条件边 condition 抛出异常时没有默认 return END → 整个 Graph 中断而非优雅退出。

## 六、退出条件的最佳组合

推荐组合使用三层防护：(1) 业务层面的条件路由显式 return END；(2) recursion_limit 硬限制 25；(3) 最外层 try/except GraphRecursionError 并记录 state.debug_trace，返回给用户一个友好的「超过最大思考步数」提示。
