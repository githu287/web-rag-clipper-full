# 开发接手笔记

2026-10-28：0.11.0 新增默认关闭的“合并分页文章”，只对智能正文从当前页向后读取，同站点明确下一页、递增页码及文章身份校验，最多 5 页/后续阶段预算 60 秒。原网页不跳转，分页来源和图片顺序合并进预览，后台按 port 管理临时标签页；关闭侧栏、取消及工作线程重启尝试清理，用户接管页保留。新增两组 Node 与真实分页浏览器回归，离线基线 827 passed / 117 subtests、13 组 Node、150 条数据格式通过。原有提取/滚动/真实 OCR 浏览器回归通过，后端与数据库未修改。本轮以本地原创样本验证，不宣称新增线上分页覆盖率，详见 [分页文章合并](PAGINATION_CAPTURE.md)。

2026-10-28：0.10.0 新增默认关闭的本地图片 OCR。所选范围图片经扩展有界读取，复用 Workspace 双凭证交给独立 CPU 子进程；长图分片，不落盘、不写库，识别结果进入可编辑预览。支持取消与页面/身份变化保护。离线 827 passed / 117 subtests、11 组 Node 和 150 条数据格式通过；真实中英文/长图、扩展 OCR 预览与原剪藏/滚动回归通过并纳入 CI。实际 API 的临时无模型 Key 空间 OCR 返回 200、错 Secret 返回 401，临时空间已清理，API/单 Worker 已启动。微信样本 5 张中 4 张输出共 4906 字，未标注逐字准确率。安装与限制见 [图片 OCR](IMAGE_OCR.md)。

2026-10-28：0.9.0 加入默认关闭的“加载更多后提取”，只对正文/整页模式滚动顶层页面或主要容器，最多 20 次/25 秒预算。保留虚拟窗口旧内容，显示进度和不完整提示，取消/完成恢复原位置与样式；章节锚点允许，SPA 路由仍拒绝旧结果。Node 增加到 10 组，后端基线仍为 813 passed / 117 subtests；新增真实扩展滚动浏览器回归与合成身份侧栏组件检查，不调用模型服务。详见 [自动加载剪藏](SCROLL_CAPTURE.md)。

2026-10-28：0.8.1 对 13 个公开页面实测，9 个文字样本通过本轮可见块/标题/源码检查，1 个图片文章受 OCR 能力限制，3 个知乎/CSDN 样本返回 403。修复 README 混入仓库界面、外置标题遗漏、高亮代码行拼接及注释删除、列表代码空行和缩进；已识别的访问限制页不再生成正文预览。新增在线审计入口与 11 份原创 DOM 模式浏览器回归，后端仍为 813 passed / 117 subtests、Node 9 组。结果不代表全站兼容率，详情见 [主流网页实测](SITE_AUDIT_2026-10-28.md)。

2026-10-27 剪藏兼容性：扩展升级为 0.8.0，加入智能正文/整页/选区/区域四种方式、本地 Readability 0.6.0 补充、有限等待与可见 iframe 提取合并。原有真实侧栏剪藏→问答→删除链路重跑通过；隔离 Chrome 中四种侧栏方式、取消保留草稿及无后端提取回归通过。离线基线后端仍为 813 passed / 117 subtests，扩展增加到 9 组；CI 新增独立 Chromium 真实提取 job。实现和未覆盖范围见 [CLIPPING_COMPATIBILITY.md](CLIPPING_COMPATIBILITY.md)。此次不增加 Workspace 凭证备份/导入功能。

2026-10-27 补充：本机已冷备份五个数据卷与原配置、逻辑备份 MySQL，并实际迁移到 `0012`。真实模型 API、独立 Chrome 扩展页面、Worker 进程退出、Redis 重启/测试队列丢失、成功后未确认、过期清理和并发配额均通过。真实验收发现删除前 Bounded 读取可能漏掉新向量，现改为 Strong 读取并补回归；8 轮跨进程立即写入/删除通过。最新离线基线 813 passed / 117 subtests passed、8 组扩展和 150 条数据格式通过。详见 [LIVE_ACCEPTANCE.md](LIVE_ACCEPTANCE.md)。

> 历史记录：下文为 2026-10-22/23 在另一开发环境的接手与验证记录，不代表当前机器运行状态。2026-10-24 整合后的迁移 head 为 `0012`；版本取舍和本次验证见 [VERSION_COMPARISON.md](VERSION_COMPARISON.md)，启动步骤见 [LOCAL_DEVELOPMENT.md](LOCAL_DEVELOPMENT.md)。

核对日期：2026-10-22（Asia/Shanghai）。代码基线：`394c85d`，分支 `main`。
本文记录本次代码阅读和本地验证结果；后续实现变化时需更新，以实际代码为准。

后续修复进展：审查报告第 1—9 项已处理，当前迁移 head 为 `0011`，最新测试为 692 passed / 115 subtests passed，扩展 7 组通过。任务持久化投递、模型互斥、文件归属、会话归属和超时已修复；配额统一到创建事务，满额可复用 URL；知识库从服务端恢复任务重试；切块重叠纳入长度预算。真实 MySQL 8 请求竞争 1 名额与 API 任务隔离验证通过。详见 `PROJECT_REVIEW.md` 顶部。

## 产品与实现边界

- 本地优先的个人知识库，主流程是网页提取、预览编辑、剪藏、检索问答。
- 扩展为 Chrome/Edge Manifest V3 + 原生 JavaScript，无 Node 构建流程；当前扩展版本 `0.7.4`。
- 文件辅助输入为 `.txt`、`.md`、`.markdown`；解析器先尝试 UTF-8-SIG，再尝试 GBK。PDF、DOCX、OCR 尚未实现。
- Workspace 是唯一业务身份与隔离边界，通过 `X-Plugin-ID` / `X-Plugin-Secret` 认证；历史 User 迁移不代表当前仍使用 User/Bearer 体系。
- 浏览器保存会话记录；当前问答 API 只使用本次问题和检索结果，未将聊天历史作为模型上下文。

## 核心链路

### 网页入库

`sidepanel.js → extractor.js / content.js → 预览编辑 → api-client.js → POST /clips/async`

API 校验凭证、模型配置和配额，将 Job 与 payload 同事务写入 MySQL，再幂等投递 Redis；Worker 定期补投提交后中断或 Redis 丢失的消息。
`IngestWorker` 领取任务，执行 `WebClipService → Chunker → DocumentIngestService → IngestService → Embedding → Milvus`。
Worker 通过 Repository 在同一事务内领取文档并绑定任务；提交任务终态后再确认 Redis 消息。

### 文件入库

`POST /documents/upload/async → LocalFileStorage → Document(PENDING) → Job + Redis → Worker → TextDocumentParser → Chunker → ingest`。
网页正文不创建本地文件；上传文件失败后通常保留，以支持重试。

### 检索与问答

`POST /rag/search → MySQL 确定可见 SUCCESS 文档 → Query Embedding → Milvus 过滤检索 → MySQL 归属/状态复核`。

`POST /rag/ask → RagService → build_context → LLM → answer + sources`。
默认取 5 个候选构造最多 4000 字符上下文；Sources 对应实际进入上下文的片段。
无可用正文时跳过 LLM。数据库、向量和模型的阻塞调用在 RAG 路径通过 `asyncio.to_thread` 执行。

## 开发入口

| 需求范围 | 主要文件 |
|---|---|
| 扩展 UI、剪藏/上传轮询、问答交互 | `extension/sidepanel.{js,html,css}` |
| 正文候选、清噪、结构化序列化 | `extension/extractor.js`、`extension/content.js` |
| HTTP、凭证、错误分类 | `extension/api-client.js` |
| 会话与 Tab 绑定 | `extension/session-store.js` |
| 网页身份与已剪藏检测 | `extension/url-utils.js`、`extension/clip-detect.js`、`backend/core/url_normalization.py` |
| API 契约与异常映射 | `backend/api/routers/`、`backend/models/*api_schema.py`、`backend/main.py` |
| 配置与依赖装配 | `backend/core/config.py`、`backend/core/di.py` |
| 入库、生命周期与删除补偿 | `backend/services/` |
| 异步任务、确认与恢复 | `backend/workers/ingest_worker.py`、`backend/tasks/redis_queue.py`、`backend/repositories/mysql/ingest_job_impl.py` |
| 检索、上下文与回答 | `backend/services/rag.py`、`rag_context.py`、`rag_answer.py` |
| 模型服务商、指纹与密钥 | `backend/models/model_provider.py`、`backend/services/plugin_service.py`、`backend/clients/`、`backend/core/security.py` |
| 持久化结构 | `backend/models/`、`backend/repositories/`、`alembic/versions/` |

更多目录说明见 `FILE_INDEX.md`，系统边界见 `ARCHITECTURE.md`，启动流程见 `README.md`。

## 修改时需要保持的约束

- 依赖方向为 Router → Service → Repository Protocol；外部实现由 DI 装配。
- MySQL 是归属、文档、任务状态与可靠投递 payload 的权威来源；Redis 负责队列和临时 payload 缓存，Milvus 负责 chunks 和向量。
- 文档状态为 `PENDING → PROCESSING → SUCCESS / FAILED`，删除使用 `DELETING`。任务状态独立为 `QUEUED → RUNNING → SUCCEEDED / FAILED`。
- 入库和删除通过原子状态领取与活动任务约束协调；处理失败不能覆盖其他操作领取的文档。
- 当前恢复机制按单 Worker 设计，不能直接增加多个 Worker 进程。
- 向量默认固定 1024 维，Milvus 使用 HNSW + COSINE；`documents.id == page_chunks.page_id`，chunk ID 为 `{page_id}_{chunk_index}`。
- re-ingest 使用确定性 ID upsert，再删除旧集合中多余的 chunks；变短的文档也必须清理尾部向量。
- 已有文档时禁止改变 Embedding 空间；同配置换 Key、换 LLM 可独立进行。
- `WorkspaceModelCredentials` 是兼容旧调用链的 `str` 子类，同时持有 Embedding / LLM 两套端点；不要将其简单转为普通字符串，否则会丢失端点配置。
- URL 规范化在浏览器与后端都有实现；普通锚点与追踪参数被剥离，`#/...` 路由须保留。两端变更应同步检查回归。
- 删除按 Milvus → 文件 → MySQL 顺序收敛，失败保留 `DELETING` 以便继续删除。
- 大部分错误返回 `{detail, type}`，异步任务族返回 `{detail, code}`。异常映射变更需同步契约测试和架构文档。
- 明文密钥与真实评测凭证不提交 Git；数据库中的模型凭证须保持加密。`APP_MASTER_KEY` 必须为 UTF-8 编码后恰好 32 字节。

## 本机验证基线

首次 Linux CI 揭示 `_resolve()` 在 POSIX 上未将反斜杠视为分隔符。已统一解析两种分隔符，仍通过 realpath/commonpath 校验根目录边界；增加混合分隔符穿越和合法子目录回归。最新本地结果为 693 passed / 117 subtests passed，扩展 7 组与 150 条数据格式校验通过。检查入口还修复 Windows GBK 日志编码，并将失败摘要写入 GitHub 检查注释以便定位。

Windows 托管 runner 的临时目录可能使用 8.3 短路径别名（如 RUNNER~1），与 realpath 返回的长路径字符串不同。两处存储测试改用 realpath/commonpath 或 samefile 验证规范路径、目录边界与文件身份，保留业务实现的真实路径安全检查。

2026-10-23 补充：新增 `.github/workflows/ci.yml` 与跨平台 `scripts/check.py`。统一命令 `python scripts/check.py all` 在本机外部服务未运行时通过：692 passed / 115 subtests passed、7 组扩展、150 条公开评测格式校验。修正 `test_ingest_recovery_fixes.py` 在 TestClient lifespan 中意外初始化真实 Milvus 的问题；只隔离该测试模块的初始化依赖，保留 SQLite Repository 及 API 路由行为。CI 配置 Linux/Windows 双平台、Python 3.11 与 Node.js 24，不需要凭证。Actionlint 1.7.12 静态验证通过；托管运行结果以 GitHub Actions 为准。

本次创建了被 Git 忽略的 `.venv/`，使用本机已有 Python `3.11.15`，安装 `requirements-dev.txt`。
默认系统 Python 为 `3.13.13`，后续在本仓库使用 `.venv/Scripts/python.exe`。

| 检查 | 结果 |
|---|---|
| `.venv/Scripts/python.exe -m pytest -q --tb=short` | 658 passed，115 subtests passed，2 warnings |
| `node extension/tests/extractor.test.js` | 通过 |
| `node extension/tests/url-utils.test.js` | 通过 |
| `node extension/tests/session-store.test.js` | 通过 |
| `node extension/tests/api-client.test.js` | 通过 |
| `node extension/tests/clip-detect.test.js` | 通过 |
| `node extension/tests/chat-request.test.js` | 通过（后续新增：8 个会话交错场景） |
| `node extension/tests/library-retry.test.js` | 通过（后续新增：15 个任务恢复场景） |
| `python -m evaluation.validate_datasets evaluation/datasets/rag_eval.jsonl evaluation/datasets/negative_eval.jsonl evaluation/datasets/isolation_eval.jsonl --allow-placeholders` | 150 条通过 |

这些单元测试使用 SQLite 与外部服务替身，本身不证明真实服务或浏览器全链路可用。
接手首轮未运行真实服务联调；随后环境配置已完成真实数据库/API/Milvus 验证，见 `LOCAL_DEVELOPMENT.md`。浏览器 DOM fixture、真实模型请求和在线 Retrieval 基线仍未运行。

## 当前发现与后续事项

1. **依赖可复现性**：按原依赖文件全新安装时，解析得到的 `setuptools 84.2.1` 不提供 `pkg_resources`，导致 `pymilvus 2.4.15` 导入失败，pytest 出现 22 个收集错误。环境配置时已在 `backend/requirements.txt` 固定 `setuptools==80.9.0`，全套测试通过。其他依赖仍使用较宽版本范围，也没有锁文件；本机安装快照在被忽略的 `.venv/installed-requirements.txt`。测试仍有 pkg_resources 弃用和 Starlette TestClient 弃用两条警告。
2. **满额重新剪藏已修复**：异步预检和 Worker 共用 URL 查重规则；已有 URL 可复用。Repository 在 Workspace 行锁内计数并创建，覆盖元数据 API 与并发创建；真实 MySQL 限额竞争已验证。
3. **联调环境已配置**：`.env`、Docker 基础服务、数据库迁移 `0011`、API 和单 Worker 已启动。真实 MySQL、Redis、API 注册/认证和 Milvus 写入/落盘/检索/删除已验证；真实模型 Key 尚待扩展内配置。详情见 `LOCAL_DEVELOPMENT.md`。
4. **启动配置**：Compose 宿主端口为 MySQL `33066`、Redis `16379`，本机 `.env` 已对齐。`18000` 被另一个项目占用，因此 API、扩展和启动文档已统一使用 `localhost:18001`。Docker Desktop 改为使用 Windows 系统代理，解决镜像直连超时；MinIO 使用 Compose 覆盖文件中的固定 PGSTY 镜像，实际与 Milvus 2.4.4 读写联调通过。
5. **维护集中点**：`sidepanel.js` 当前 2446 行，多个 UI 流程集中在一个文件；后续扩展交互时可按具体需求逐步拆分。部分代码注释仍描述历史 Phase，应核对实际实现。
6. **评测解释**：150 条数据校验通过仅表示数据结构通过检查；公开 chunk 标注仍待部分人工重标，尚不能据此宣称真实检索质量或完整 chunk 级基线。

接手阶段未改业务代码；随后按用户要求完成本地环境配置，调整了依赖声明、扩展端口、Compose 覆盖文件与启动文档，并迁移本地数据库。后续高优先级修复见本文顶部，提交记录以 Git 历史为准。
