"""
api 包：HTTP API 层（Phase 0 §4.2 约定位置）。

职责边界：
- 接收 HTTP 请求 + Pydantic 参数校验；
- 通过 FastAPI Depends 注入 Service（IngestService / RagService）；
- 调用 Service 方法 + 将结果包装为 API Response Schema 返回；
- 将 Service 抛出的业务异常转换为 HTTPException。

不负责：
- 业务流程编排（Service 层职责）；
- 外部 SDK 调用（pymilvus / openai 由 Repository / Client 内部隔离）；
- Milvus / 百炼连接管理（由各层内部惰性建立）。
"""
