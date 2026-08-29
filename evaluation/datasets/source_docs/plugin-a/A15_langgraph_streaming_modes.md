# LangGraph Streaming 流式输出三种模式

用户体验良好的 Agent UI 必须支持流式输出。LangGraph 提供 stream_mode、astream_events、astream_log 三种递进的流式能力。

## 一、模式一：stream (默认) — 节点级增量

`graph.stream(input, config)` 返回一个生成器，每次 yield 一个节点完成后的增量 state 更新：

```python
for update in graph.stream(input, config):
    # update = {"call_model": {"messages": [AIMessageChunk(...)]}}
    node_name, payload = next(iter(update.items()))
    print(f"[{node_name}] 产生 {len(payload)} 条更新")
```

这是最粗粒度的 Streaming：每个 token 不会实时吐出，只有整个节点 invoke 完成后才 yield 一次。适用于简单「步骤指示器」UI。

## 二、模式二：astream_events — Token 级细粒度

`graph.astream_events(input, config, version="v2")` 是 LangChain 0.2 新引入的事件流。事件类型包含：
- `on_chat_model_start / stream / end`：LLM 每一个 token chunk。
- `on_tool_start / end`：工具执行起止。
- `on_retriever_start / end`：Retriever 开始与完成。
- `on_chain_start / end`：任何 Runnable。

每个事件的字段：`event`、`name`、`run_id`、`tags`、`metadata`、`data`。可以精确地把 AIMessageChunk.content 流式渲染到前端 <textarea>，并在工具执行期间显示 loading spinner。

## 三、模式三：astream_log — 完整树结构

`graph.astream_log(input, config)` 在内部每一步输出一个 Patch 对象，描述自上次以来 LangChain Run 树的变化。适合 LangSmith 风格的 Debug UI：可以回放每个 Run 的输入输出、耗时、错误堆栈。缺点是数据量大，不推荐面向用户的 UI 使用。

## 四、前端集成示例（SSE）

FastAPI 后端：

```python
@app.post("/graph/stream")
async def stream_graph(body: dict):
    async def event_generator():
        async for event in graph.astream_events(body["input"], config, version="v2"):
            if event["event"] == "on_chat_model_stream":
                yield f"data: {json.dumps({'type':'token','chunk':event['data']['chunk'].content})}\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

前端 EventSource 监听后逐 token 追加到 DOM。

## 五、常见错误

1. stream 模式误以为能拿到 token → 拿不到，只能拿到节点快照。
2. astream_events 的 version 参数省略 → 不兼容，必须显式 version="v2"。
3. 工具输出中有换行符直接塞进 SSE data 行 → Base64 编码后再传输。
4. Streaming 中断但 Checkpoint 未保存 → 使用 interrupt_after 每个节点结束后确保 checkpoint 写完成。
5. recursion_limit 过大导致前端长时间等待没有心跳 → 前端加 30s keep-alive 定时器。
