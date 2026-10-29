# 云端视频总结（0.13.0）

2026-10-29。第一版已实现，所有模型推理通过百炼 Qwen-Omni HTTP API 执行。FFmpeg 只负责本地媒体解码、压缩、分段，不部署本地模型。

## 使用

1. 重新加载 `extension/`，打开侧栏“视频”。
2. 在“云端视频模型”填写百炼 Chat Completions 服务地址、Omni 模型及该地域的 API Key。配置独立于原来的 Embedding 和聊天模型，后端使用现有 `APP_MASTER_KEY` 加密；Key 不回传、不存入浏览器存储，保存后清空输入框。留空保留已有 Key。
3. 上传 MP4、MOV、WebM、MKV 或 AVI，或粘贴 B站/抖音视频页、分享链接或分享文案。默认最多 100 MiB、30 分钟，不会静默截断视频。
4. 查看任务进度；关闭后重开面板可找回服务端任务。处理成功后查看、编辑总结或下载 Markdown。
5. 勾选核对确认后，才把编辑后的 AI 总结作为 Markdown 上传至现有知识库。入库仍需配置 Embedding 和问答模型。原视频不入库；总结是模型生成的资料，不能代替原视频证据。

本机已有百炼配置的 Workspace 已复用其现有 Key，单独配置并验证 `qwen3-omni-flash`，原 Embedding/问答配置未更改。其他 Workspace 需要自行配置。新增型号的地域、开通权限和地址以控制台及[官方 Qwen-Omni 文档](https://help.aliyun.com/zh/model-studio/qwen-omni)为准，不能把普通文本模型名称填入视频模型。

## 支持范围与结果含义

| 输入 | 当前行为 |
|---|---|
| 本地视频 | 有界上传、校验容器与实际时长、逐段云端分析声音和画面 |
| B站视频页 | 解析公开单个稿件，保留 `p` 分 P，取得视频与音轨后合并 |
| `b23.tv`、抖音分享短链 | 校验跳转，再交给对应平台解析器 |
| 抖音视频页 | 尝试取得公开媒体；平台可能要求登录或拒绝解析，不保证任意链接成功 |
| 直播、合集、登录/付费/DRM 内容、任意其他网站或文件直链 | 当前不支持；提示上传用户已有的视频文件 |

平台解析使用固定版本 yt-dlp，只启用 Bilibili/Douyin 适配器，不读取浏览器 Cookie 或登录凭证。媒体下载仅接受相关平台域名的公网 HTTPS 文件，每次连接检查全部 DNS 地址，并连接校验后的固定 IP；重定向也受校验。HLS、未知外部媒体域名与无法取得完整音轨的素材会失败，不把标题或简介冒充视频内容。平台改变时需更新适配器或白名单并重新测试。

每段默认最长 90 秒，视频压缩为最大宽度 720px、12fps，保留音轨。原生音视频以 Base64 传入云端，单段原始媒体最多 7 MiB，确保编码后小于供应商 10 MB 限制。多段再通过云端生成整体摘要。结果包含整体摘要、分段内容、声音/画面依据、模型、累计 token 用量。章节时间由真实分段边界计算，**不是逐句转写时间或精确事件定位**。尚未实现独立 ASR 逐字稿、说话人分离、精确字幕或任意秒跳转。

压缩、采样与模型能力可能遗漏小字、短暂画面、提示音或细节；静态讲解画面的分析也可能过度推断，使用前应核对。没有音轨时明确标注只依据画面。取消会阻止最终结果写入并停止后续分段，已发出的云端请求可能仍产生用量。失败最多手动处理 3 次；进程中断不会自动重复收费请求。

## 部署

在原有环境额外安装并升级数据库，API/Worker 升级前停止旧进程、保留数据库和主密钥备份：

```powershell
.venv/Scripts/python.exe -m pip install -r backend/requirements.txt -r backend/requirements-video.txt
.venv/Scripts/python.exe -m alembic upgrade head
```

当前 head 为 `0013`。新增 `video_model_configs` 和 `video_jobs`；不修改原来的模型配置、Document 或入库队列契约。新结果字段在 MySQL 使用 LONGTEXT，避免长视频中文总结超过 TEXT 上限。

除原 API 和单个入库 Worker，再单独启动一个视频 Worker：

```powershell
.venv/Scripts/python.exe -m backend.workers.video_worker
```

视频队列保存在 MySQL，使用原子领取、租约和心跳防止旧进程覆盖新结果；与 Redis 入库任务分开。超过 1 分钟仍排队时面板会提示检查视频服务。临时素材和派生媒体都在 `UPLOAD_DIR/video/`，成功即清理、取消和删除会清理，失败原素材默认保留 24 小时供重试；结果默认保留 7 天。Worker 定期和新提交时执行过期清理；进程停机期间无法执行定时清理，恢复后对账删除崩溃残留。每个 Workspace 最多 3 个未完成任务、30 条历史记录，可主动删除。

删除 Workspace 会先封闭视频写入、停止任务并清理配置与素材，再沿现有流程删除文档与空间。删除失败会恢复空间可访问状态，已完成的资源删除不回滚。

可选配置见 `.env.example`：`VIDEO_MAX_UPLOAD_BYTES`、`VIDEO_MAX_DURATION_SECONDS`、`VIDEO_SEGMENT_SECONDS`、`VIDEO_RETENTION_HOURS`、`VIDEO_RESULT_DAYS`、`VIDEO_FFMPEG`。默认使用 PATH 的 FFmpeg，否则使用 imageio-ffmpeg 自带二进制，不在运行时下载模型。可用性检查不等于云端权限验证；保存视频配置不会发起模型请求。

## API

全部要求现有 `X-Plugin-ID` 与 `X-Plugin-Secret`，归属由认证决定，不接受调用方传 Workspace ID。

| 方法与地址 | 用途 |
|---|---|
| `GET/PUT/DELETE /videos/config` | 安全读取、加密保存、移除独立视频配置 |
| `GET /videos` | 最近的任务与预览结果 |
| `POST /videos/upload` | multipart 上传并返回 202 |
| `POST /videos/link` | `{ "url": "视频页或分享文案" }`，返回 202 |
| `GET /videos/{id}` | 状态、阶段、进度、用量和结果 |
| `POST /videos/{id}/retry` | 手动重试未过期失败任务 |
| `POST /videos/{id}/cancel` | 取消任务 |
| `DELETE /videos/{id}` | 删除任务与原素材 |

异空间读取、重试、取消和删除统一 404。`VideoError` 使用现有 `{detail,type}` 结构；云端认证错误映射 502，不会误清除 Workspace 身份。

## 验证记录

2026-10-29 本机验证：

- 全量后端回归 867 passed、119 subtests passed；13 组扩展 Node 检查和 150 条公开评测数据格式校验通过。两条既有依赖弃用警告不影响结果。

- 单元/集成测试覆盖加密、配置独立性、大小限制、分块请求体、配额、隔离、租约、中断重试、过期/取消/空间删除、真实 FFmpeg 处理、迁移升降级及真实 SDK 流式协议。云端边界在回归中使用替身，不收费。
- `scripts/check_video_browser.py` 加载真实无头 Chrome 扩展，执行真实 API、SQLite、上传和 FFmpeg；6 个流程通过。该脚本的云端与最终入库边界是明确的替身。
- 另用现有百炼凭证执行真实云端小样本：中文绿萝浇水讲解配静态红色画面，正确读到讲解和画面；纯提示音样例出现漏识别。模型对静态画面仍可能过度解释，不代表内容准确性全面验证。
- 真实 MySQL `0013`、API、视频 Worker、云端视频总结、Redis 入库 Worker、Milvus 与问答完整链路通过；临时 Workspace 删除 204，测试文档和素材清理。
- 真实 B站样例解析、视频/音轨下载、合并与解码通过（554.12 秒、约 26.45 MB，确认有音轨）；实测修复了高码率超限时未选择较小版本的问题。抖音样例返回访问/解析失败，已验证明确错误与本地上传退路。该结果不代表所有平台链接可用，未对该 B站长视频执行全量收费总结。

本机升级前逻辑备份位于 Git 忽略且限制访问的 `.venv/backups/before-0013-20261029T030544Z/`。个人凭证、备份、运行截图和测试结果不提交 Git。CPU 性能、30 分钟上限视频的成本/延迟和所有平台样本还未全面验收。
