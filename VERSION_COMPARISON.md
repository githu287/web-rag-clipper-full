# 本地与 GitHub 版本整合记录

日期：2026-10-24。整合采用两版互补，不整体覆盖任一版本。

## 比较基线与保留策略

- 共同基线：`394c85d`。
- 本地优化备份：`bf05f4f`，本地分支 `codex/project-reliability-optimizations`。保留原优化内容，便于回溯。
- GitHub 基线：`034e6e62c1c94081884e59f694d11bebe491c210`，共同基线之后有 7 个提交。
- 整合分支：`codex/integrated-project`，合并两条提交历史；不重写远端提交。

| 范围 | 采用内容与理由 | 主要实现 |
|---|---|---|
| 配额与模型一致性 | 保留远端 Workspace 行锁、事务内统一配额及模型指纹校验，覆盖并发创建和配置切换 | `backend/repositories/mysql/workspace_lock.py`、`impl.py`、`plugin_impl.py` |
| 文件归属 | 保留远端元数据路径限制、共享历史文件删除保护、跨平台路径归一化 | `backend/models/document_api_schema.py`、`backend/services/document_delete.py`、`backend/storage/local.py` |
| 入库可靠性 | 以本地恢复流程为主，合并远端原子领取及模型校验；同时扫描数据库 RUNNING 和 Redis processing，文档已成功时补记任务成功，避免重复向量化 | `backend/workers/ingest_worker.py`、`backend/repositories/mysql/ingest_job_impl.py` |
| 投递与期限 | 结合两版 MySQL 持久化、Lua 去重和有限批次补投；采用本地正文固定期限、过期清理及事务提交后接收结果保护 | `backend/services/ingest_job.py`、`backend/tasks/redis_queue.py`、`backend/tasks/payload.py` |
| 客户端连接 | 保留本地有界 LRU 租约缓存和退出清理，活动调用释放后再关闭被淘汰连接 | `backend/clients/client_cache.py`、`embedding.py`、`llm.py`、`backend/main.py` |
| 上传与删除 | 保留本地有界读取、上传路由线程池、取消请求时准备与提交连续执行、删除同步 I/O 移出事件循环；合并远端归属和配额保护 | `backend/api/upload.py`、`backend/api/routers/documents.py`、`backend/services/workspace_delete.py` |
| 扩展任务恢复 | 合并远端按来源查找服务端任务、失败重试，以及本地轮询网络重试和旧身份隔离；本地存储失败不覆盖服务端成功结果 | `extension/sidepanel.js`、`extension/tests/library-retry.test.js`、`extension/tests/sidepanel-jobs.test.js` |
| 会话与等待预算 | 保留远端会话快照归属、旧凭证 401 保护、按操作动态等待预算和响应正文超时 | `extension/api-client.js`、`extension/sidepanel.js`、`backend/core/request_budget.py` |
| 切块与检查 | 保留远端包含 overlap 的长度限制、Linux/Windows CI 和统一离线检查入口 | `backend/chunkers/recursive.py`、`.github/workflows/ci.yml`、`scripts/check.py` |

## 冲突处理与运行约定

两版均新增过编号 `0011` 的迁移，内容不同。GitHub 已发布的 `0011_durable_ingest_payload.py` 保持不变，新增 `0012_ingest_payload_deadline.py`。在线升级检查字段和索引是否存在，兼容两种 `0011` 数据库结构，并保留已有正文期限；无期限历史网页正文按创建时间加默认 7 天回填。离线 SQL 以远端标准 `0011` 为起点。

保留远端 API 默认端口 `18001`，扩展配置、host permissions 和启动文档一致。保留固定摘要的 MinIO 开发镜像覆盖配置，本轮未验证实际拉取或服务启动。将远端机器的服务运行记录明确标为历史，并重写可跨机器复用的 `LOCAL_DEVELOPMENT.md`。

不读取或更改个人 `.env`，不安装依赖，不迁移真实数据库，不重启已有服务。升级运行环境时先停止 API 与 Worker，再执行 `alembic upgrade head`，随后启动 API 和单个 Worker，重新加载扩展。

## 已验证

Windows / 现有 Python 3.11 环境：

```powershell
.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --tb=short -o faulthandler_timeout=20
.venv\Scripts\python.exe -B scripts/check.py extension datasets
```

- 后端：811 passed、1 skipped、117 subtests passed；2 条依赖弃用警告。
- 扩展：10 个顶层脚本语法检查、8 组 Node.js 回归通过。
- 评测数据：150 条公开样本格式通过，允许运行时 ID 占位符。
- 迁移回归覆盖两种 `0011` 结构升级、已有期限保留、历史期限回填及 MySQL SQL 编译；未对实际 MySQL 执行迁移。

本次没有验证真实模型请求、浏览器端到端、MySQL 行锁、Redis Lua 或 Milvus 服务；既有离线替身测试通过不等价于上述实测。GitHub Linux/Windows 工作流结果以 Actions 实际运行结果为准。

## 已知边界与后续优先级

1. 实际环境升级与冒烟：`0012` 迁移、单 Worker、扩展重新加载，以及一次上传/剪藏/问答/删除完整流程。
2. 故障恢复实测：Redis 重启、Worker 中断、成功入库后确认失败、正文过期和并发配额，使用独立测试 Workspace 验证。
3. 保持单 Worker；当前恢复机制不支持多个 Worker 并行。Lua 去重针对同一任务，未为 HTTP 重提请求提供通用幂等键。
4. 上传有界读取发生在 multipart 解析之后，不能替代服务器总请求体限制；删除仍为跨存储可重试补偿，不是分布式事务。
5. 真实检索质量、复杂网页提取及生产部署能力仍需分别验证；不因本次整合宣称完成。
