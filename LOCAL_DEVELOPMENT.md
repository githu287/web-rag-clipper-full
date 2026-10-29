# 本地开发与升级

2026-10-27 本机已完成备份、真实 MySQL `0012` 升级、模型/服务链路与隔离浏览器扩展页面验收，API 与单 Worker 已启动；实际结果、备份位置、发现的向量删除一致性修复及验证边界见 [LIVE_ACCEPTANCE.md](LIVE_ACCEPTANCE.md)。这是当次运行记录，重新使用前仍应检查服务状态。

在当前仓库根目录执行以下命令。本文是可复用的操作指南，不表示基础设施或后台进程已经运行。2026-10-24 整合只验证离线测试，未安装依赖、读取个人 `.env`、启动服务或迁移实际数据库；结果见 [VERSION_COMPARISON.md](VERSION_COMPARISON.md)。远端原开发机器的验证记录作为历史保留于 `DEVELOPMENT_NOTES.md` 和 `PROJECT_REVIEW.md`。

## 环境与配置

需要 Python 3.11、Docker Compose，以及运行扩展检查所需的 Node.js（CI 使用 24）。浏览器扩展直接加载 `extension/`，无 npm 安装或打包步骤。

首次建立 Python 环境：

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
```

已有 `.venv` 时先复用。依赖清单固定 `setuptools==80.9.0`，用于兼容 pymilvus 的 `pkg_resources`。仅首次部署时从 `.env.example` 创建个人 `.env`，按本机服务填写配置；不要覆盖现有文件。Compose 默认 MySQL 端口为 `33066`，Redis 为 `16379`。

如需图片文字识别，另执行 `.venv/Scripts/python.exe -m pip install -r backend/requirements-ocr.txt` 后重启 API。模型随依赖安装，运行时不下载，不需要额外 API Key；刷新扩展后勾选“识别图片中的文字”。操作与限制见 [图片 OCR](IMAGE_OCR.md)。

`APP_MASTER_KEY` 必须为 UTF-8 编码后恰好 32 字节；已有数据库务必保留原主密钥，否则已加密模型凭证无法解密。模型 API Key 在扩展设置页填写，不需放入旧版 `BAILIAN_API_KEY`。个人配置、上传文件及私有评测数据均不提交 Git。

## 启动和升级

先停止旧 API 与 Worker，避免混用新旧数据库结构；普通入库和视频处理分别运行一个独立 Worker。启动 Docker Desktop 后运行：

```powershell
docker compose up -d --wait --wait-timeout 180
.venv/Scripts/python.exe -m alembic upgrade head
```

当前 head 为 `0013`（新增视频配置与任务表，见 [视频总结](VIDEO_SUMMARY.md)）。保留已发布的远端 `0011`；`0012` 在线检查是否已存在过期字段与索引，兼容此前本地的 `0011` 变体。已有正文期限不变；历史 WEB_CLIP 正文缺失期限时按创建时间加 7 天回填，因此旧正文可能已经过期。离线 SQL 假设起点是标准远端 `0011`。迁移无法找回旧版本已丢失的正文；升级前应处理完在途任务，并按原有数据库备份流程保留备份。

`docker-compose.override.yml` 沿用远端固定摘要的 PGSTY MinIO 镜像；本轮未重新验证其可拉取性或真实 Milvus 读写。

在两个终端分别启动：

```powershell
.venv/Scripts/python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 18001
```

```powershell
.venv/Scripts/python.exe -m backend.workers.ingest_worker
```

视频总结需额外安装 `.venv/Scripts/python.exe -m pip install -r backend/requirements-video.txt`，并在独立终端运行 `.venv/Scripts/python.exe -m backend.workers.video_worker`。视频模型在扩展“视频”面板配置，全部推理在线上完成。

API 文档地址为 `http://localhost:18001/docs`。默认端口与 `extension/config.js`、`extension/manifest.json` 一致；更换地址时同时更新这两处。不要重复启动 Worker。

## 加载扩展

1. 在 `chrome://extensions/` 或 `edge://extensions/` 开启开发者模式。
2. 加载当前仓库中的 `extension/` 目录；已加载则点击重新加载并重新打开 Side Panel。
3. 创建或使用自己的 Workspace，在设置中分别配置 Embedding 和问答模型；Embedding 输出必须为 1024 维。

公开服务商目录提供模型请求等待预算，扩展缓存 1 分钟；修改超时配置需重启 API。客户端超时不保证取消后端操作，应先检查结果，再决定是否重提。

## 离线检查与运行状态

```powershell
.venv/Scripts/python.exe scripts/check.py all
```

也可分别选择 `backend`、`extension`、`datasets`。测试禁止加载个人 `.env`，使用 SQLite 和外部服务替身；150 条公开数据只校验格式，不表示真实检索质量。真实 MySQL/Redis/Milvus、模型调用及浏览器端到端需另行验证。

以下命令仅用于自己的运行环境，整合过程未执行实际数据库迁移：

```powershell
docker compose ps
.venv/Scripts/python.exe -m alembic current
```

API 和 Worker 默认向所启动终端输出日志；本仓库不保证存在历史机器上的 `.venv/runtime/` 后台日志或验证脚本。
