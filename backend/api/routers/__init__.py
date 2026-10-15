"""
api.routers 子包：FastAPI 路由器集合。

当前路由器（按 prefix）：
- /plugins   Plugin Workspace 身份体系与模型服务配置
- /clips     网页剪藏入库（同步与异步任务）
- /documents 文档列表、详情、chunk 与删除
- /ingest    页面 chunk 入库编排
- /jobs      异步 ingest 任务状态查询与重试
- /rag       检索与问答

各 Router 通过 main.py 的 create_app() 中 app.include_router(...) 注册。
"""
