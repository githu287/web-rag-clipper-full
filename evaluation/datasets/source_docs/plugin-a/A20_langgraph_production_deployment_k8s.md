# LangGraph 生产部署：Docker、K8s 与水平扩展

把 LangGraph Agent 从本地 Demo 推广到生产需要工程化改造。本文总结 Docker 打包、Kubernetes 部署、横向扩展、限流熔断等关键实践。

## 一、Dockerfile 生产打包要点

四阶段构建：builder-base → poetry-install → app-stage → runtime-stage。runtime 使用 slim-bullseye 非 root 用户。关键命令：

```dockerfile
FROM python:3.11-slim-bookworm AS runtime
RUN groupadd -r appuser && useradd -r -g appuser appuser
WORKDIR /app
COPY --from=poetry-install /app/.venv /app/.venv
COPY backend backend
ENV PATH="/app/.venv/bin:$PATH"
USER appuser
EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
```

禁止用 root 用户运行容器；禁止 `pip install` 不在 lockfile 中的包；Healthcheck 必须是实际业务 probe（`GET /healthz`），不是简单的 curl 8000。

## 二、Kubernetes 部署四件套

1. **Deployment**：replicas ≥ 2，PodAntiAffinity 避免两个副本落同一 node。
2. **Service**：ClusterIP + Ingress。
3. **HPA**：基于 `http_requests_per_second` 或 `active_graph_threads` 扩容，Min 2, Max 20。
4. **PDB**：`minAvailable: 1` 保证滚动升级期间至少 1 个 Pod 存活。

## 三、状态型服务的横向扩展注意事项

LangGraph + PostgresSaver 是「软状态」服务：计算层无状态，真实状态在 Postgres 和 Redis。理论上可以无限水平扩展，但实际运行必须处理以下三个共享资源冲突：

1. **同 thread_id 并发写 checkpoint** → Postgres 层利用 (thread_id, checkpoint_id) 主键做乐观锁，冲突自动 retry。
2. **Interrupted resume 消息重复投递** → 幂等键：update_state 的签名要包含幂等 ID。
3. **多实例共享 Checkpointer connection** → 必须用连接池，禁止每个请求开新连接。

## 四、限流与熔断

推荐使用 `slowapi` + Limiter：
- 每个 tenant_id 每分钟最多 60 次 /rag/ask。
- 每个 tenant_id 并发（inflight）最多 10。

熔断用 `pybreaker`：当百炼 API 在最近 1 分钟失败率 > 40% 时，熔断 30 秒直接走降级响应，防止雪崩。

## 五、可观测性三件套

1. **Prometheus Metrics**：rag_ask_total（Counter）、rag_ask_duration_seconds（Histogram buckets 0.1/0.5/1/2/5/10）、rag_retrieval_recall5（Gauge 采样估算）。
2. **Structured Logging**：JSON 日志（loguru），每笔 request_id 贯穿全链路。
3. **Tracing**：OpenTelemetry SDK + OTLP Exporter，把 LangSmith tracing 镜像同步一份到 Jaeger 做基础设施级 tracing。

## 六、滚动升级与兼容性

Graph 拓扑变更（新增节点、删除边）会导致旧 checkpoint（旧 state schema）在新版本上反序列化失败。两种方案：
- 方案 A：部署时先把旧版本 Pod 上所有未完成的 thread 跑完（drain 模式），再滚动升级。
- 方案 B：引入 versioned state schema，新版本提供迁移函数把旧版 TypedDict 升级到新版。

推荐方案 A + 最大 thread 超时 30 分钟 drain。
