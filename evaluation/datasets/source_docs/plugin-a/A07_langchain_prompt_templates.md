# LangChain PromptTemplate 与 ChatPromptTemplate

Prompt 是 LLM 应用的第一道质量防线。LangChain 提供 PromptTemplate（字符串模板）和 ChatPromptTemplate（对话消息列表模板）两种核心抽象。

## 一、PromptTemplate

PromptTemplate 基于 Python 标准 `str.format()` 风格的占位符。

```python
from langchain_core.prompts import PromptTemplate
template = "请用中文回答以下问题。请只使用给定上下文：{context}\n\n问题：{question}\n回答："
prompt = PromptTemplate.from_template(template)
value = prompt.format(context="LangGraph 是 LangChain 的有状态图库。", question="LangGraph 是什么？")
```

必须保证传入 format 的 kwargs 集合与模板占位符集合完全相等，否则抛出 KeyError。可以用 `prompt.input_variables` 检查。

## 二、ChatPromptTemplate

Chat Prompt 由多轮消息组成。ChatPromptTemplate 支持三种消息占位符：

```python
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

chat_prompt = ChatPromptTemplate.from_messages([
    ("system", "你是一个资深 {role}。只能使用 Context：{context}。"),
    MessagesPlaceholder(variable_name="history"),
    ("human", "{question}"),
])
```

每个消息元组 `(role, content_template)` 中 role 可选 system、human、ai。MessagesPlaceholder 用于动态注入历史消息列表。

## 三、少样本提示（Few-shot）

Few-shot 推荐使用 `FewShotChatMessagePromptTemplate`，它会把示例自动格式化插入：

```python
examples = [
    {"input": "2+2", "output": "4"},
    {"input": "2+3", "output": "5"},
]
example_prompt = ChatPromptTemplate.from_messages([
    ("human", "{input}"), ("ai", "{output}"),
])
few_shot = FewShotChatMessagePromptTemplate(examples=examples, example_prompt=example_prompt)
final = ChatPromptTemplate.from_messages([
    ("system", "你是一个计算器。只输出数字。"),
    few_shot,
    ("human", "{input}"),
])
```

## 四、Prompt 复用与版本管理

建议每个 Prompt 版本独立存为 `.txt` 或 `.json` 文件，使用 Git 管理变更。运行时用 `PromptTemplate.from_file(path)` 加载。绝对禁止把 Prompt 硬编码到多个 Service 文件中，造成多处漂移。

## 五、常见陷阱

1. 使用 f-string 拼 f"你好 {name}" 而非 PromptTemplate → 无法用 `.partial` 注入。
2. 中文 Prompt 的大括号被 JSON 混淆 → 用 `{{` 和 `}}` 转义。
3. 忘记把 MessagesPlaceholder 对应的 history 变量从 state 中取出来 → 抛出 ValidationError。
4. System 消息太长导致后续 human 被截断 → 使用 system+content 限制在 2000 字符以内。
5. Few-shot 示例里包含花括号未转义 → 每个示例用 `{example}` 时双重转义。
