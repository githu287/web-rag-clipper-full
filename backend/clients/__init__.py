"""
clients 包：外部服务客户端封装层（Phase 0 §4.2 约定位置）。

职责边界：
- 仅封装外部 API 调用（百炼 Embedding / 未来 LLM / 重排等）；
- 不负责业务编排、文档解析、Milvus 写入、Cache、Service。

本阶段（Phase 2.5 Step 2）仅落地百炼 Embedding 客户端。
"""
