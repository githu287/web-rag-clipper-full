"""
api.routers 子包：FastAPI 路由器集合。

当前路由：
- ingest : POST /ingest/page      （页面 chunk 入库编排）
- rag    : POST /rag/search        （RAG 检索）

各 Router 通过 main.py 的 create_app() 中 app.include_router(...) 注册。
"""
