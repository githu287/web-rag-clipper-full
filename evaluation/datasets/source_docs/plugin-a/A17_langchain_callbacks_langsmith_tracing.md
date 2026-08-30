# LangChain Callback 与 LangSmith Tracing 集成

想要回答「为什么我的 RAG 召回了这个错误的 chunk？」这类问题，必须有完整的 tracing。LangChain 通过 Callback 机制支持全链路 Tracing，官方平台是 LangSmith。

## 一、Callback 的三个入口

任何 Runnable 都可以在 invoke/stream 时传入 callbacks。Callback 对象继承 `AsyncCallbackHandler` 或 `CallbackHandler`，并覆盖 on_llm_start、on_llm_new_token、on_llm_end、on_tool_start、on_tool_end、on_retriever_start、on_retriever_end 等钩子。

三种注入方式：
1. 调用时注入：`chain.invoke(x, config={"callbacks": [handler]})`。
2. 构造时注入：`RunnableLambda(f).with_config(callbacks=[handler])`。
3. 环境变量全局注入：`LANGCHAIN_TRACING_V2=true` + `LANGCHAIN_API_KEY` → 所有 Runnable 自动发送到 LangSmith。

## 二、LangSmith 的三个核心概念

- **Project**：一个应用或一次实验。同一 Project 下的 runs 才能对比。
- **Run**：一次 Runnable 的调用。Run 形成树形结构：Chain 是父 Run，其子 LLM、Retriever 是子 Run。
- **Dataset**：人工标注的测试集。支持 `client.run_on_dataset()` 一键在 Dataset 上跑全链路评估。

## 三、与 LangGraph 的集成

LangGraph 编译后也是 Runnable，因此同样会自动把每个节点作为 Run 记录。每个节点名 → Run.name；节点入参 state → Run.input；节点返回增量 → Run.output。条件边不会产生 Run，但会在 parent Run 的 metadata 中写入 `langgraph:next: ["target_node"]`，用于可视化边方向。

## 四、自定义 Metadata 打标

Tracing 必须区分租户、用户、会话。正确方式在 Config 中注入：

```python
config = {
    "configurable": {"thread_id": "t_123"},
    "tags": ["tenant-corp-a", "user-u-42", "exp-v3-prompt"],
    "metadata": {"tenant_id": "corp-a", "version": "2024.08.11"}
}
graph.invoke(..., config=config)
```

tags 用于 UI 筛选；metadata 键值对会写入每一个 Run，便于 LangSmith Dataset 评估时按租户聚合。

## 五、Callback 的反模式

1. 在 CallbackHandler 中做耗时 IO → 阻塞整个链路。正确做法：Handler 只把消息投递到异步队列（Kafka/Redis Stream），不做同步 DB 写入。
2. 自定义 Handler 里 try/except 空吞异常 → 导致 Handler 静默失效。必须至少打 logger.exception。
3. 每个 Handler 实例单例复用导致线程安全问题 → 每次调用使用独立 Handler。
4. 忘了关闭 Tracing 导致性能下降 → 生产默认关闭 V2 tracing，仅在小流量采样上打开。

## 六、LangSmith Dataset 集成 RAG 评估

LangSmith `run_on_dataset()` 方法专门用于 RAG 评估：把数据集（question、gold_contexts、gold_answer）上传后，系统自动跑你的 RAG Chain，并运行自定义 evaluator（如 Context Precision、Faithfulness）。这是 Phase 3.7 外的可选扩展，但在当前项目我们选择自建 evaluation harness 以便完全控制指标定义。
