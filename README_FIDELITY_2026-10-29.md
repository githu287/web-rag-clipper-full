# 大型 README 代码与列表保真修复（0.12.1）

接续 [0.12.0 发现的待查项](COLLAPSED_CONTENT.md)。对真实 yt-dlp README 快照逐块比较，确认代码块没有少 5 个，而是 47 个块中的 5 个内容发生了变化；列表文字也没有整段删除，而是子列表与后续说明被重排。

## 修复

1. `normalizeStructuredText` 曾在整篇文字中替换 Unicode 零宽连接字符，使代码里的组合 emoji 被拆开。现在代码围栏内保留原字符；正文也保留 ZWJ/ZWNJ，不再拆开组合字符。
2. 代码围栏内连续空行曾与普通段落一起压缩，行尾空白也会被删除。现在代码负载保留空行、制表符、不换行空格及其他 Unicode 字符和行尾空白；普通正文仍清理多余空行。CRLF/CR 继续统一为 LF，代码边界原有的首尾空行处理不变。
3. 列表序列化曾先收集普通段落、最后追加所有子列表，导致“标题 → 子列表 → 说明”变成“标题 → 说明 → 子列表”。现在按 DOM 顺序输出，按父项标记宽度计算子列表及续段缩进；以代码开头的列表项使用独立围栏，避免代码内容被当作普通正文归一化。

未增加站点特例，后端、数据库、模型配置和文档存储未修改。原有剪藏文档需要用户重新提取并确认保存，同一规范化 URL 仍更新原文档。

## 真实网页结果

实际无头 Chrome 加载扩展，直接访问 [yt-dlp/yt-dlp README](https://github.com/yt-dlp/yt-dlp)，使用相同审计脚本和原有比较门槛：

| 检查 | 旧版记录 | 0.12.1 |
|---|---:|---:|
| 参考正文块保留 | 660/661 | 661/661 |
| 代码块源码比较 | 42/47 | 47/47 |
| 标题、首尾参考内容及噪声项检查 | 未整体通过 | 通过 |

代码比较沿用既有的换行、行尾及边界空行规范化，未放宽门槛；另用原创测试直接断言 Unicode、连续空行和行尾空白的精确保留。线上块覆盖率是本次参考 DOM 的比较结果，不是整站覆盖率或人工逐字准确率。

开启“加载更多后提取”另测，同样达到正文块 661/661、代码 47/47；原有滚动预算及不完整提醒仍按实际停止原因显示，不因此宣称后续未加载内容完整。

掘金、博客园、GitHub Readability README、Vue、MDN 五个原有公开样本复查通过，参考正文块 247/247、代码 78/78。MDN 的内嵌演示内容和总输出长度会随加载情况变化，不能单凭长度认定完整。

大型 README 已加入 `extension/tests/site-samples.json`，当前定义共 14 个公开样本；这不表示本轮重新测试了其余所有样本。在线审计不纳入 CI 成败，不登录或绕过访问限制。正文、快照与机器报告在忽略目录 `.venv/runtime/readme-fidelity-20261029/`，不提交第三方全文。

## 回归与复跑

13 组 Node 回归通过，新增代码连续空行、Unicode/行尾空白、正文空行与列表续段缩进检查。原有两个会删除代码行尾空白的预期已按新保真行为更新。真实浏览器回归增加原创多层列表、子列表前后段落顺序、列表首项代码、源码精确保留和原 DOM 不变检查，原有 12 份站点 DOM 模式及折叠预览/选区/区域/iframe/取消回归通过。

```powershell
.venv/Scripts/python.exe scripts/check.py extension
.venv/Scripts/python.exe scripts/check_capture_browser.py
.venv/Scripts/python.exe scripts/audit_capture_sites.py --only github-yt-dlp
.venv/Scripts/python.exe scripts/audit_capture_sites.py --only github-yt-dlp --auto-scroll
.venv/Scripts/python.exe scripts/audit_capture_sites.py --only juejin-closure cnblogs-decorator github-readme vue-guide mdn-table
```

本次不调用模型或重新入库；滚动、分页、OCR 本地流程继续由既有 CI 覆盖。
