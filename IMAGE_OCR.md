# 图片文字识别（0.10.0）

2026-10-28。侧栏新增“识别图片中的文字”，默认关闭。开启后读取当前剪藏范围内的 HTTP(S) 图片，交给项目后端的本地 CPU 模型识别。中英文结果按图片顺序附在正文末尾，标注“图片文字识别”，可编辑后再确认保存。OCR 不创建文档、任务或向量，也不需要 Embedding/LLM Key；最终入库仍使用原有模型配置。

## 使用

在已有 Python 3.11 环境中安装可选依赖，随后启动或重启 API：

```powershell
.venv/Scripts/python.exe -m pip install -r backend/requirements-ocr.txt
.venv/Scripts/python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 18001
```

刷新扩展，打开侧栏，选择剪藏方式并勾选“识别图片中的文字”。图片文章可用智能正文，识别不全时选择指定区域。开启“加载更多后提取”时，快照保留图片地址，避免虚拟列表回收节点后丢失。选区只收集 DOM Range 内的图片。识别期间显示进度，侧栏可取消并保留之前编辑的预览。切换标签页、路由或工作空间时拒绝旧结果。

依赖缺失或部分图片失败时，普通正文仍可预览，诊断区说明未完成部分；纯图片且未识别出文字时明确报错，不生成空文档。失败图片不自动重试。

## 实现与限制

- 固定 `rapidocr-onnxruntime==1.4.4`，使用包内 PP-OCRv4 模型和 ONNX Runtime CPU；模型随可选依赖安装，请求时不下载。来源：[RapidOCR v1.4.4](https://github.com/RapidAI/RapidOCR/tree/v1.4.4/python/rapidocr_onnxruntime)、[PyPI 发行页](https://pypi.org/project/rapidocr-onnxruntime/1.4.4/)。依赖及模型沿用上游许可证，不在仓库另行复制权重。项目支持 Python 3.11。
- 最多前 8 个去重地址；每图 6 MiB，成功读取的图片累计 24 MiB。单图读取等待 12 秒、后端识别 40 秒，整个 OCR 阶段 2 分钟。达到上限保留已有文字并提示，可分区域处理。正文加 OCR 共享 500000 字符上限。
- 检查真实格式和尺寸：PNG/JPEG/WebP/GIF/BMP，最多 2000 万像素、最长边 40000。宽度超过 1600 会缩放并提示。长图按 1536 像素高、160 像素重叠切片，用检测框中心分配重叠区域，保留合法重复行；边界识别仍可能有误。
- 分数低于 0.5 的行舍弃，0.5–0.8 提醒核对。动画只识别首帧并提醒。不理解图表语义或重建复杂表格；手写、倾斜、低清晰度和复杂背景可能误识别或遗漏。复杂多列版式不保证阅读顺序。
- 图片来自清理后的正文副本，冻结 `currentSrc` 或常见懒加载地址。CSS 背景、Canvas、SVG、`data:`、`blob:`、关闭 Shadow DOM 尚不支持。需要 Cookie/Referer 的防盗链图片可能读取失败，不绕过访问控制。
- 扩展读取图片时使用 `credentials: omit`、不发送 Referer，不向图片站点发送 Workspace Header。图片二进制只发给配置的项目后端；远程后端意味着图片会发往该服务器。后端不接受图片 URL、不抓取远程图片、不保存图片文件；确认剪藏后识别文字才进入入库链路。浏览器及 HTTP 层可能有自身缓存和缓冲。
- `POST /ocr/image` 复用双 Header 认证，原始图片 body 有界流式读取，不以 MIME 声明代替解码。响应包含 text、尺寸、分片数、warnings 和引擎名。每个 API 进程同时运行一个独立识别子进程；忙时 429，超时 504，缺依赖或引擎失败 503。客户端断开后轮询取消并终止子进程。多 API 进程各有一个容量，不构成全局配额。
- OCR 路由专用错误为 FastAPI `detail: {code, message}`：`OCR_TOO_LARGE`、`OCR_INVALID_IMAGE`、`OCR_BUSY`、`OCR_UNAVAILABLE`、`OCR_FAILED`、`OCR_TIMEOUT`、`OCR_CANCELLED`。身份错误继续复用原全局 handler。图片地址和二进制不写入正文，结果以图片序号与说明标记来源。

## 验证

```powershell
.venv/Scripts/python.exe scripts/check.py all
.venv/Scripts/python.exe scripts/check_ocr.py
.venv/Scripts/python.exe scripts/check_ocr_browser.py
```

后端回归覆盖认证、有界读取、错误契约、并发拒绝、超时/取消杀进程、非法图片、像素限制、分片边界和低置信度。Node 覆盖凭证隔离、图片数量/大小、去重、部分失败、身份变化、取消和总字符上限。`check_ocr.py` 用真实模型读取仓库内原创中英文与长图 PNG，验证三段长图的边界、顺序与合法重复行。

本地离线结果为 827 passed / 117 subtests、11 组 Node 和 150 条数据格式通过。另在实际 MySQL 认证的运行 API 中创建临时 Workspace（无模型 Key），图片 OCR 返回 200，错误 Secret 返回 401，随后删除临时 Workspace；此检查不做入库或问答。依赖一致性 `pip check` 通过。API/单 Worker 已启动，这仅是当次运行记录，后续使用仍应核查进程状态。

`check_ocr_browser.py` 加载真实扩展，使用生产 OCR 路由与真实本地模型，仅数据库认证服务替换为临时身份；验证默认关闭、正文/选区范围、可编辑预览、取消保留旧稿、纯图片文章、懒加载地址及虚拟列表图片累积。测试不调用收费模型或写入数据库，不代替完整入库/问答验收。原有提取和滚动浏览器回归也已通过。CI 的 Linux/Windows 后端任务运行真实 OCR 样本，Linux Chromium 运行 OCR 预览流程。

本轮复查此前的[微信图片文章样本](https://mp.weixin.qq.com/s/v8g0jI-c49RsihsaPBgeRQ)：真实扩展找到 5 张图片，原 DOM 提取 198 字；模型在 4 张中输出合计 4906 字，另 1 张未发现文字，其中 3 张长图切为 2/2/3 段。一张动画首帧存在低置信度提醒。这是读取/识别链路的观察，没有逐字标注准确率，不代表微信全站兼容。原始识别文本只留在忽略目录 `.venv/runtime/ocr-live/`，不提交仓库。
