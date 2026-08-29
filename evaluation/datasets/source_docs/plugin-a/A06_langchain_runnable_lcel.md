# LangChain Chain 与 Runnable 协议详解

LangChain 0.2 之后，几乎所有核心组件都统一实现了 Runnable 协议。理解 Runnable 协议是掌握 LangChain 的基础。

## 一、Runnable 的核心方法

Runnable 统一提供 6 个核心方法：`invoke(input, config)`、`ainvoke`、`batch`、`abatch`、`stream`、`astream`。任何继承 `Runnable[Input, Output]` 的对象都可以被一致方式调用。

```python
from langchain_core.runnables import RunnableLambda

add_one = RunnableLambda(lambda x: x + 1)
add_one.invoke(5)          # 6
add_one.batch([1, 2, 3])   # [2,3,4]
```

## 二、Runnable 的五类组合子

1. `pipe` 或 `|` 操作符：线性组合。`prompt | model | parser` 是最经典的 LCEL（LangChain Expression Language）链。
2. `RunnableParallel`（别名 `RunnableMap`）：并发执行多个子 Runnable。`{"a": runnable_a, "b": runnable_b}`。
3. `RunnablePassthrough`：原样透传输入，通常用于 RunnableParallel 中保留原输入。
4. `RunnableBranch`：基于输入键路由到不同 Runnable，与 LangGraph 条件边类似但不支持循环。
5. `RunnableSequence`：多个 Runnable 用 pipe 显式构造的列表对象，可通过 `.steps` 检查。

## 三、Chain 的典型形态：RAG Chain

一个标准 RAG Chain 由四个步骤组合：

```python
retrieval_chain = (
    {"context": retriever | format_docs, "question": RunnablePassthrough()}
    | prompt
    | llm
    | StrOutputParser()
)
```

第一步用 RunnableParallel 把输入拆成 context 和 question 两个分支；context 分支先调 retriever 再 format；question 分支透传；两路合并后传入 prompt。

## 四、Config 的两个核心字段

- `configurable`：动态参数注入，如 `{"configurable": {"llm_model": "qwen-plus"}}`。
- `callbacks`：回调链，LangSmith tracing、日志、限流均通过此接口注入。

## 五、常见错误

1. RunnableLambda 的 lambda 抛出异常但未包装 → 用 `with_fallbacks([fallback])` 加降级。
2. 字符串模板直接 `|` 拼接 → 必须先包装成 `RunnablePassthrough.assign(question=lambda x: x)` 再用 ChatPromptTemplate。
3. stream 模式下输出 AIMessageChunk 而非 str → 末尾追加 `StrOutputParser()`。
4. Configurable 字段名字拼写错误 → 用 `.config_schema()` 打印合法字段列表。
5. 误用 batch 导致内存爆炸 → 显式指定 `batch([...], config={"max_concurrency": 5})`。

## 六、为什么不推荐使用旧的 Chain 类

LangChain 0.1 及以前存在 `LLMChain`、`RetrievalQA` 等具体类，但它们都在 0.2 被标记为 deprecated，因为 LCEL Runnable 的组合性更强、性能更高、类型更安全。新项目一律使用 LCEL。
