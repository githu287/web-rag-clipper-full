# LangGraph Agent Tool Calling 机制实战

Tool Calling 是现代 Agent 与外部世界交互的核心机制。LangGraph 通过与 LangChain BaseTool 接口深度集成，提供了完整的工具调用闭环。

## 一、BaseTool 的最小实现

继承 BaseTool，实现 `_run(self, *args, **kwargs)` 同步方法和可选的 `_arun` 异步方法。name 字段必须是合法的 Python 标识符，description 字段必须写清楚工具用途和参数含义——LLM 根据 description 决定是否调用该工具。

```python
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

class SearchInput(BaseModel):
    query: str = Field(description="搜索关键词，必须是短语而非完整句子。")

class WikipediaSearch(BaseTool):
    name = "wikipedia_search"
    description = "从维基百科查询指定关键词的摘要。"
    args_schema = SearchInput

    def _run(self, query: str) -> str:
        # 真实实现调用 requests 到 api.wikipedia.org
        return f"[wikipedia] {query}: ...摘要..."
```

## 二、把工具绑定到 LLM

所有现代 LLM（qwen-plus、gpt-4o、deepseek-chat 等）支持原生 Tool Calling。LangChain 提供 `.bind_tools(tools)` 将工具定义序列化为 LLM API 可理解的 JSON schema：

```python
tools = [WikipediaSearch(), Calculator()]
llm_with_tools = llm.bind_tools(tools)
ai_msg = llm_with_tools.invoke(messages)  # 可能返回 ai_msg.tool_calls 列表
```

每条 tool_call 包含 name、args、id 三个字段。id 是 LLM 生成的 UUID，ToolMessage 必须把这个 id 原样回传，用于多工具场景下 LLM 对齐。

## 三、Graph 中的执行顺序

标准四步循环：
1. call_model：llm_with_tools.invoke(state["messages"]) → AIMessage，可能含 tool_calls。
2. should_continue：条件边判断是否有 tool_calls → 是走 call_tools，否走 END。
3. call_tools：遍历 tool_calls，对每个 (name, args, id) 用 ToolNode 执行，返回 ToolMessage 列表。
4. add_edge(call_tools, call_model)：工具结果追加到 messages，重新进入 LLM 思考下一步。

## 四、ToolNode 的三个特性

- 自动 name→tool 映射：按 tool.name 查找，找不到抛 ValueError。
- 并发执行：多个 tool_calls 用 ThreadPoolExecutor 并行执行（同步工具）或 asyncio.gather（异步）。
- 错误处理：单工具异常时捕获为字符串并作为 ToolMessage content 返回，附加 `status=error` tag，保证 Graph 不中断。

## 五、实战错误清单

1. description 写得模糊 → LLM 不知道何时调用 → 准确率低。正确做法：明确写「用于查询 X。当问题涉及 Y 时使用。不要用于 Z。」
2. args_schema 字段缺少 description → LLM 随机填参数。
3. ToolMessage.tool_call_id 传错 → LLM 无法关联，表现为工具结果被忽略。
4. 忘记用 bind_tools 而手动拼 System Prompt 描述工具 → 不支持原生 Tool Calling 的模型才这样做。
5. 并行工具调用全部失败后返回空 messages → 必须把失败信息写进 ToolMessage，否则 LLM 陷入重复调用死循环。
