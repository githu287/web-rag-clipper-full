# LangChain OutputParser 结构化输出完全指南

把 LLM 的自由文本输出解析成稳定的结构化对象，是 Agent 能否可靠调用工具的基础。LangChain 提供三类解析器：String、Pydantic 和 JSON。

## 一、StrOutputParser

最简单的解析器：

```python
from langchain_core.output_parsers import StrOutputParser
chain = model | StrOutputParser()
result = chain.invoke(...)  # 返回纯字符串
```

它负责把 BaseMessage 子类（AIMessage、AIMessageChunk 等）统一取出 `.content`。在 Streaming 场景是必备。

## 二、PydanticOutputParser（推荐）

把 LLM 输出解析为 Pydantic v2 BaseModel：

```python
from pydantic import BaseModel, Field
from langchain_core.output_parsers import PydanticOutputParser

class Answer(BaseModel):
    thinking: str = Field(description="推理思路")
    answer: str = Field(description="最终回答")
    sources: list[int] = Field(description="引用的来源编号列表")

parser = PydanticOutputParser(pydantic_object=Answer)
format_instructions = parser.get_format_instructions()
```

使用方式：把 `format_instructions` 拼入 System Prompt，LLM 按照其描述输出 JSON。parser.parse(text) 返回强类型的 Answer 对象。

## 三、JsonOutputParser（轻量替代）

JsonOutputParser 不需要 Pydantic Model，只要求输出能被 json.loads 解析的字符串，返回 dict。适用于原型快速开发，但缺乏字段类型校验。

## 四、带自动重试的 OutputFixingParser

任何 Parser 都可以包装 OutputFixingParser：当第一次 parse 失败时，它会把错误信息重新喂给 LLM，请求修正输出。

```python
from langchain.output_parsers import OutputFixingParser
robust_parser = OutputFixingParser.from_llm(parser=parser, llm=model, max_retries=2)
```

建议生产环境默认用 OutputFixingParser 包裹 PydanticOutputParser，重试次数设置为 2。

## 五、五个实战经验

1. Pydantic Model 的字段 description 必须详细且准确——它直接进入 Prompt，决定解析成功率。
2. Enum 类型必须写清所有合法选项描述。
3. Optional[X] 字段要在描述中明确说明「如果信息不存在则返回 null」。
4. 避免 list[PydanticModel] 过深嵌套——越复杂 LLM 越容易输出格式错误，必要时拆成多次调用。
5. 把 parser 抛错的原始文本、异常信息完整记录到日志，用于后续迭代 Prompt。

## 六、常见问题：带 ```json  fences

部分模型习惯把 JSON 用三引号 ```json ... ``` 包裹，PydanticOutputParser 默认不识别。解决方法：在 Prompt 的 format_instructions 末尾追加一句「禁止使用 Markdown 代码块，只输出裸 JSON 字符串」。
