# LangGraph StateGraph 核心定义与使用方法

LangGraph 是 LangChain 生态中用于构建有状态多步 Agent 流程的底层框架。其最核心的抽象类是 `StateGraph`，它负责定义 Agent 的计算拓扑：节点、边与状态容器。

## 一、StateGraph 的实例化

StateGraph 在构造时必须接收一个状态 Schema。该 Schema 既可以是一个 TypedDict，也可以是一个 Pydantic BaseModel。推荐使用 TypedDict 以便灵活追加字段。常见的字段包括 messages、next_step、tool_result、user_input 等。典型示例如下：

```python
from typing import TypedDict, Annotated, Sequence
from langchain_core.messages import BaseMessage
import operator

class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], operator.add]
    next_step: str
    tool_result: dict
```

随后调用 `StateGraph(AgentState)` 即可得到一个 Graph 实例。需要强调的是，LangGraph 的状态是不可变副本传递：每个节点函数接收当前状态，返回一个字典形式的增量，然后由 Graph 引擎根据 Annotated 中的 reducer（如 operator.add）自动合并。

## 二、节点（Node）的注册

StateGraph 通过 `add_node(name, func)` 方法注册节点。name 必须是唯一的字符串，func 是一个同步或异步可调用对象。约定节点函数只做一件单一职责的事，例如 call_model、call_tools、should_continue。返回值必须是一个字典，其键必须与 State Schema 中声明的字段子集匹配。

值得注意的是，LangGraph 不允许节点直接调用另一个节点。所有节点之间的跳转必须通过边来声明。这个设计使得整个 Graph 可以被静态分析和序列化。

## 三、边（Edge）的三类形态

LangGraph 提供三种边：普通边 add_edge(start, end)、条件边 add_conditional_edges(source, condition, mapping)、以及入口 END 标记。条件边是 LangGraph 与普通 DAG 的核心差异：condition 函数返回一个字符串键，mapping 将该键映射到下一个节点名。常见模式是 call_model 后根据 tool_calls 是否存在，跳转到 call_tools 或 END。

## 四、编译与运行

在声明完节点与边后，必须调用 `graph = workflow.compile()` 得到可执行的 CompiledGraph。调用方式支持 `graph.invoke(input)`、`graph.stream(input)` 和 `graph.ainvoke(input)` 三种。invoke 返回最终聚合状态；stream 以生成器形式逐节点产出快照，这是实现 Streaming UI 的基础。

## 五、常见陷阱

1. 忘记设置入口 `set_entry_point("call_model")` 会抛出 GraphValidationError。
2. 条件边 mapping 遗漏 "end" 路径会导致死循环。
3. State reducer 使用默认 replace 语义而非 operator.add，可能导致 messages 被覆盖而非累加。
4. 返回字典键名与 TypedDict 字段名不一致时，编译阶段不会报错但运行时会出现 KeyError。
5. 同步节点函数不能直接 await 异步 LangChain Runnable，必须统一用同步 invoke 或把节点函数定义为 async。
