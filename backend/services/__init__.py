"""
services 包：业务编排层（Phase 0 §4.2 约定位置）。

职责边界：
- 编排业务流程（Ingest re-ingest 三步、RAG 检索 + post-filter）；
- 调用 EmbeddingClient + MilvusRepository（Protocol）；
- DTO 转换（chunk 文本 + embedding → ChunkVector）。

不负责：
- Milvus Client / OpenAI Client 创建（由 Repository / Client 内部管理）；
- HTTP 请求处理（API 层职责）；
- 文档解析（上游职责，传入的是已切分的 chunk 文本列表）。
"""
