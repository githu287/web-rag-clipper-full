# LangChain Agent 与 LangGraph Agent 的选型对比

很多初学者会问：langchain.agents 中的 AgentExecutor 和 LangGraph 自定义 Graph 应该怎么选？本文从六个维度做系统对比。

## 一、灵活性

AgentExecutor 是一个固定的 ReAct 循环：LLM 思考 → 选工具 → 执行 → 重复。只能改 tool 列表和 Prompt，不能控制流程节点本身。例如「工具执行结果不满意时先向用户确认再决定下一步」这样的 Human-in-the-loop 流程几乎无法实现。

LangGraph 可以任意定义节点、边、条件分支、退出条件、人工审核、多 Agent 协作等任意拓扑。代价是开发者需要自己声明节点、不能开箱即用。

## 二、可观测性

AgentExecutor 只有一个 agent_step 回调，隐藏了大量内部逻辑。调试「为什么 LLM 第二次思考又调用了同一个工具」非常困难。

LangGraph 每个节点是显式的：每个节点进出都可以打回调、写 trace、存 checkpoint。stream_mode="updates" 可以把中间结果精确到「哪个节点修改了哪个字段」这一级别。

## 三、状态管理

AgentExecutor 内部状态是 AgentFinish / AgentAction 混合对象，外部很难精确访问。想要持久化中断需要自己保存完整的 intermediate_steps。

LangGraph 由开发者显式定义 TypedDict state，所有字段都可见、可序列化、可 Checkpointer 持久化，并天然支持 interrupt/resume。

## 四、工具调度

AgentExecutor 天然支持并发工具调用（parallel_tool_calls 参数），以及 StructuredTool 的多种参数模式。但对「工具调用顺序有严格依赖」的场景（例如必须先拿 auth_token 再调用 api_detail），需要在 Prompt 里强制约束。

LangGraph 可以把工具调用拆成多个节点：先 auth_node → 拿到 token 写入 state → 再 call_api_node → 最后 process_node。依赖关系在 Graph 结构层面保证，远比 Prompt 指令可靠。

## 五、上手成本

AgentExecutor 只需 `initialize_agent(tools, llm)` 一行，5 分钟出 Demo。适合快速原型验证。

LangGraph 至少需要写：State TypedDict → 3~5 个节点函数 → 节点注册 → 普通边 + 条件边 → compile。代码量大约是 AgentExecutor 的 5 倍。

## 六、生产稳定性

AgentExecutor 最大的问题是「神秘死循环」：当 LLM 在某一轮因为 Prompt 漂移而重复调用同一个工具时，开发者无法干预。虽然 max_iterations 可以强行停止，但它是硬超时，不是优雅退出。

LangGraph 可以通过条件边在每一轮写入 state.step_count，达到阈值时把 next_step 设置为 "end" 并返回友好的「无法在有限步数内解决」回答。

## 七、最终建议

MVP / Demo 阶段：AgentExecutor + 3~5 个工具。
生产系统 / 需要 Human-in-the-loop / 需要可追溯：LangGraph。
两者可以混合：在 LangGraph 的某个节点内部调用一个 AgentExecutor 完成子任务（例如代码解释器子 Agent）。
