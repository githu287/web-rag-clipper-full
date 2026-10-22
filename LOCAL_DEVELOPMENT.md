# 本机开发环境

配置日期：2026-10-22（Asia/Shanghai）。项目目录：`C:\Users\Administrator\Desktop\Plugin\web-rag-clipper-full`。

## 已配置内容

- `.venv/` 使用 Python 3.11.15，运行与开发依赖已安装，包含 pip。
- `backend/requirements.txt` 固定 `setuptools==80.9.0`，兼容 pymilvus 2.4.15 使用的 `pkg_resources`。
- `.env` 已从模板生成，MySQL 端口为 `33066`，Redis 为 `16379`，已随机生成 32 字节 `APP_MASTER_KEY`。该文件已被 Git 忽略；保留现有主密钥，避免已有模型配置无法解密。
- Docker Desktop 使用 Windows 已有系统代理。修改前的 Docker 设置备份位于 `%APPDATA%\Docker\settings-store.before-web-rag-*.json.bak`。
- MySQL、Redis、etcd、MinIO 和 Milvus 已启动；本地数据库已迁移至 `0011`，包含可靠任务投递 payload。
- API 与单 Worker 当前以隐藏后台进程运行；未注册 Windows 系统自启动。

## 地址与服务

| 服务 | 本机地址 |
|---|---|
| API / Swagger | `http://localhost:18001/docs` |
| MySQL | `localhost:33066` |
| Redis | `localhost:16379` |
| Milvus gRPC / Health | `localhost:19530` / `http://localhost:9091/healthz` |
| MinIO API / Console | `http://localhost:19000` / `http://localhost:19001` |

`18000` 已被另一项目使用，本项目使用 `18001`；扩展 `config.js` 与 `manifest.json` 已同步。

原 `minio/minio` 镜像拉取失败，这一问题也见 [Milvus 上游记录](https://github.com/milvus-io/milvus/issues/53430)。
`docker-compose.override.yml` 会自动覆盖 MinIO 为本机已有的 PGSTY 分支镜像：

```text
pgsty/minio:RELEASE.2026-03-25T00-00-00Z
sha256:a72bf37c235a83a73890d2a46c5b36801fed61c335175e0396070bf84a8bbb98
```

该分支的来源见 [PGSTY 仓库](https://github.com/pgsty/silo)。这里只验证了本项目本地开发所需的 Milvus 对象存储读写兼容性。

## 重启后启动

启动 Docker Desktop，然后在项目根目录执行：

```powershell
docker compose up -d --wait --wait-timeout 180
.venv\Scripts\python.exe -m alembic upgrade head
```

在两个终端分别启动 API 与 Worker。当前后台进程仍运行时不要重复启动，尤其不能同时运行多个 Worker。

```powershell
.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 18001
```

```powershell
.venv\Scripts\python.exe -m backend.workers.ingest_worker
```

如需重新安装 Python 依赖：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

## 加载扩展与模型配置

1. 打开 `chrome://extensions/` 或 `edge://extensions/`，开启开发者模式。
2. 加载已解压的扩展目录 `C:\Users\Administrator\Desktop\Plugin\web-rag-clipper-full\extension`；若已加载，点击重新加载。
3. 打开 Side Panel，创建自己的 Workspace。
4. 在设置中分别填写 Embedding 和问答模型的服务商、模型与 API Key。Embedding 输出需为 1024 维。

模型凭证由后端加密保存，不需要填写旧版 `.env` 的 `BAILIAN_API_KEY`。本次未配置真实模型 Key，也未执行收费模型请求。

## 验证与日志

- 后端测试：675 passed、115 subtests passed；扩展 6 组 Node.js 回归通过。
- 真实 MySQL 连接与 `0011` 版本通过；Redis 返回 PONG。并发模型切换锁、文件引用保护与 Redis 补投/去重均已实测。
- API 的文档页、23 个业务操作、Workspace 注册、认证、文档列表与测试 Workspace 清理通过。
- Milvus `page_chunks` 已创建，向量维度为 1024；临时 Collection 的 insert、flush、search、delete 通过，临时资源已清理。
- Worker 启动日志确认已进入任务消费循环；真实模型入库与问答仍需填写 Key 后验证。

本次后台 API 与 Worker 的日志保存在 `.venv/runtime/` 下的 `api.*.log` 与 `worker.*.log`。
环境验证脚本为 `.venv/runtime/check_environment.py`，安装版本快照为 `.venv/installed-requirements.txt`，均被 Git 忽略。

本轮高优先级修复说明见 `PROJECT_REVIEW.md` 顶部。`POST /documents` 不再接受非空本地路径；上传文件仍使用上传接口。升级其他环境时需先停止 API/Worker、迁移至 `0011` 再启动。真实 MySQL/Redis 专项验证脚本为 `.venv/runtime/verify_priority_live.py`。

会话与超时修复无需新增迁移：更新后重启 API，并在浏览器扩展管理页点击重新加载，再重新打开 Side Panel。`GET /plugins/model-providers` 返回公开等待预算，随 `BAILIAN_EMBEDDING_TIMEOUT_SECONDS`、`BAILIAN_LLM_TIMEOUT_SECONDS`、`BAILIAN_MAX_RETRIES` 计算；修改这些环境变量需重启 API，扩展的预算缓存有效期为 1 分钟。默认问答和双模型验证最长等待 540 秒，检索和单 Key 验证 240 秒；普通 JSON 请求 30 秒、上传 120 秒。客户端超时不会自动重试，也不保证取消服务端处理；模型配置等待超时后，先重新读取设置确认结果再决定是否重提。

常用检查：

```powershell
docker compose ps
.venv\Scripts\python.exe -m alembic current
.venv\Scripts\python.exe -m pytest -q
```
