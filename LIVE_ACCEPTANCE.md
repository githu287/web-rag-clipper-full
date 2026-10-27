# 本机升级与真实链路验收

日期：2026-10-27，Asia/Shanghai。升级起点为合并提交 `612c38a`。以下是本机当次结果，不保证后续服务始终在线。

## 备份与升级

- 升级前 API、Worker 与项目五个容器均未运行，数据库版本为 `0011`。
- 冷备份 MySQL、Redis、Milvus、MinIO、etcd 五个数据卷，并保存原 `.env`（含原 APP_MASTER_KEY）。目录仅授予当前用户与 SYSTEM 访问，存放在被 Git 忽略的 `.venv/backups/before-0012-20261027T025925Z/`。
- 每份归档检查 gzip CRC、tar 成员并计算 SHA-256，记录于 `manifest.json`。MySQL 启动恢复后、迁移前，另做 `database-before-0012.sql` 一致性逻辑备份。升级前上传目录不存在；验收生成的文件已删除。
- 执行 `alembic upgrade head`，实际 MySQL 已为 `0012`，确认 `payload_expires_at` 字段与索引存在。未覆盖 `.env`，最终校验其字节摘要与备份一致。
- API 启动于 `http://127.0.0.1:18001`；单 Worker 已运行，MySQL/Redis/MinIO/Milvus 健康，etcd 运行正常。故障测试期间暂停主 Worker，只运行一个隔离测试 Worker，完成后恢复主 Worker。

备份文件校验不等价于完整恢复演练。备份与密钥不提交 Git，也未上传 GitHub。

## 验收结果

| 范围 | 实际验证 | 结果 |
|---|---|---|
| 真实模型与服务 | 独立测试 Workspace 通过现有模型配置验证，调用真实 Embedding 与问答模型；MySQL → Redis → Worker → Milvus | 通过 |
| 网页与文件 | 网页剪藏、文本上传、规范化 URL 原位更新、来源受限检索 | 通过 |
| 问答 | 测试文章编号 `527`，真实回答正确且包含对应文档来源 | 通过 |
| 失败重试 | 无法以 UTF-8/GBK 解码的测试文件失败；服务端按文档找回任务，修复该合成源文件后重试，第二次执行成功 | 通过 |
| 浏览器扩展 | 隔离 Chrome 配置中加载真实扩展，注册、content script 提取、预览、确认剪藏、问答与来源、上传和界面删除 | 通过 |
| 浏览器轮询 | 对文件任务状态查询注入两次连接失败，页面自动重连并显示成功 | 通过 |
| Worker 中断 | 实际子进程在入库中途退出，MySQL 保留 RUNNING/PROCESSING，重启后同一文档恢复成功 | 通过 |
| 成功后未确认 | 实际子进程在文档/向量成功、Job 成功写入前退出；恢复只修补任务元数据，没有再次生成向量 | 通过 |
| ACK 失败 | 注入 Redis 确认失败，保留 processing；下一进程恢复确认，没有重复生成向量 | 通过 |
| Redis 重启 | 在 Worker 中断后，仅删除测试命名空间的队列数据，再实际重启项目 Redis 容器，任务从 MySQL 恢复成功 | 通过 |
| 正文过期 | 测试正文设置 1 秒期限，过期后真实 Worker 清理 payload、任务可观察地失败，HTTP 重试返回 409 | 通过 |
| 并发配额 | 真实 MySQL 中剩余 1 名额，8 个并发创建只有 1 个成功，其他 7 个被配额拒绝 | 通过 |
| 删除一致性 | 8 轮跨进程真实向量写入后立即经 API 删除，最终无残留向量 | 通过（见下方修复） |

真实模型验收使用现有配置的独立测试 Workspace，不更改原 Workspace 配置。故障场景使用合成 Embedding 输出，仍执行真实 MySQL、Redis、Milvus 与后台处理链路，不为故障重跑额外调用收费模型。

浏览器通过 Chrome 支持的扩展调试接口加载真实扩展，使用独立用户目录、无头模式，在扩展页面运行 UI；为对应原生侧栏的网页上下文，测试脚本显式激活本地文章 Tab 并调用实际上下文加载函数。没有伪造 chrome API 或成功业务响应，但未验证原生侧栏容器的展开/关闭、浏览器工具栏入口及用户日常配置文件。

## 验收发现并修复的问题

Milvus 集合的默认读取一致性是 Bounded。Worker 刚完成写入后，API 的独立客户端可能查不到新 chunk ID；删除流程便可能删掉 MySQL 文档，却遗漏向量。本次在临时测试文件上观察到该残留，随后按精确 ID 清理。

`PyMilvusRepositoryImpl.query_page_chunks()` 现在显式使用 Strong 一致性读取，让删除和重新入库在枚举旧向量时看到已确认的跨进程写入。仍按精确 chunk ID 删除，不改变归属或状态校验。新增回归模拟 Bounded 看不到新向量，验证删除仍执行向量清理；真实 8 轮立即写入/删除也全部通过。Strong 读取可能等待服务同步，本次未进行性能压力测试。

## 最终状态与验证边界

- 原有 2 个 Workspace 保留；文档 0、任务 0，与验收前基线一致。临时 Workspace、测试任务、上传文件及向量全部清理；Milvus 强一致查询确认无残留向量。
- `.venv/Scripts/python.exe scripts/check.py all`：813 passed、117 subtests passed，8 组扩展回归、10 个顶层 JS 语法检查、150 条公开数据格式校验通过；2 条既有依赖弃用警告。
- API 文档可访问，API 与单 Worker 保持运行。后台日志和 PID 在本机 `.venv/runtime/`。
- 本地验收脚本与结果为 `.venv/runtime/acceptance_api.py`、`acceptance_faults.py`、`acceptance_browser.py`、`acceptance-*-result.json`；仅用于这次本机环境，未提交 Git。故障脚本必须先暂停主 Worker，不能与其并行执行。
- 本次不构成真实知识库质量评测、生产性能或高可用认证。Workspace 凭证导入恢复、原生侧栏容器验收、完整备份恢复演练仍待后续工作。
