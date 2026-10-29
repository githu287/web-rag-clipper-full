# 折叠正文与完整性提示（0.12.0）

2026-10-29。网页上的“展开全文”可能只改变显示样式，也可能触发请求、导航或登录。本版本区分已在 DOM 中的原生折叠正文与明确尚未展开的隐藏区域，不自动点击网站按钮。

## 行为

- 原生 `<details>` 中已加载的正文此前通常已能提取；现在将 `details` 和 `summary` 按块序列化，避免摘要和后面的行内正文粘连。保留标题、列表、代码和图片说明，显式隐藏节点仍按原规则清理。
- 清理后的结果含有非空、尚未打开的原生折叠块时，预览提醒“已包含原生折叠块中已加载的正文，请核对预览”。只有摘要、正文被清理为空的块不计数；图片正文也计数。
- 在所选正文范围内，若可访问的“展开全文/显示更多/Read more”等明确按钮使用 `aria-expanded="false"` 和 `aria-controls` 指向同范围中的隐藏节点，预览提醒在原网页展开后重新提取。按目标节点去重，不因多个按钮重复计数；已显示的目标、缺失目标、普通按钮、禁用控件、表单、导航、侧栏和评论中的控件不触发该提醒。
- 用户手动展开并重新提取后，新增可见正文进入预览，相应提醒消失。CSS 截短但 DOM 文字已保留的情况，不仅凭高度变化就判断漏采。

诊断字段为 `included_collapsed_count`、`pending_collapsed_count`，提醒为 `collapsed_content_included` / `collapsed_content_pending`。计数针对本次清理后的结构或选定范围，不代表超长截断后所有折叠块都已完整保存；达到 50 万字符仍显示原有截断提醒。滚动、分页及 iframe 合并沿用现有提醒累积，不将诊断写入文档正文。

原网页 DOM、折叠状态、URL 不由此功能改变，不发送点击、toggle 事件或额外展开请求。原生折叠正文已被网站交付到当前 DOM，不会绕过登录、付费或验证码。任意自定义脚本、缺少明确 ARIA 关联的按钮、跨范围/跨 Shadow DOM 关联和点击后才加载的正文仍不能自动判断或获取。选区模式只处理选中范围，不为范围外控件报待展开提醒。

## 验证

真实 Chrome 加载扩展的原创本地页面通过：

- 智能正文/整页模式保留折叠摘要、正文和 CSS 截短文字；隐藏属性内容及隐藏原生块不混入。
- 空折叠块不计数，图片折叠块计数；重复按钮指向同一目标只提示一次。
- 提取前后 HTML、URL、原生折叠状态及点击/toggle 计数保持不变；随后人工点击测试页面按钮展开，重新提取可获得新增文字并清除提醒。
- 实际扩展预览展示中文提醒，手动展开后重新提取会更新。此 UI 检查使用合成身份，不执行入库、OCR 或模型请求。
- 原有选区、区域、跨域 iframe、延迟加载、取消/导航和 12 份原创站点模式回归通过；13 组 Node 回归通过。

公开网页另测：

| 样本 | 结果与范围 |
|---|---|
| [DevOps Exercises README](https://github.com/bregman-arie/devops-exercises) | 真实扩展捕获前 5 个关闭的原生问答，摘要及每块首个非空段落/列表项 5/5 保留；正文 DOM 和原始开闭状态未变。没有据此认定全部 363 个折叠块均逐字验证通过 |
| GitHub Readability README、Vue 基础、MDN table（原有样本 URL 见 `extension/tests/site-samples.json`） | 参考正文块 149/149、代码 41/41 保留；三个样本检查通过 |
| [yt-dlp 大型 README](https://github.com/yt-dlp/yt-dlp) | 没有原生折叠块；全文审计为 `content_mismatch`：参考块 660/661、代码 42/47。导出已提交 0.11.3 的扩展独立对照，结果相同，属于原有列表/代码保真待查项，本轮未宣称通过 |

线上参考比较与人工准确率不同，不代表全站兼容。原文、截图和机器报告保留于忽略目录 `.venv/runtime/collapsed-20261029/`，不提交第三方全文或凭证。后端、数据库和模型配置未改，本次未重新执行模型入库问答。

```powershell
.venv/Scripts/python.exe scripts/check.py extension
.venv/Scripts/python.exe scripts/check_capture_browser.py
.venv/Scripts/python.exe scripts/audit_collapsed_site.py
.venv/Scripts/python.exe scripts/audit_capture_sites.py --only github-readme vue-guide mdn-table
```

`audit_collapsed_site.py` 只读检查最多 10 个原生折叠块，默认上述真实 DevOps 页面和前 5 块；无后端、无模型请求，不纳入 CI 成败。普通网页审计增加 `--extension-dir`，可用旧版 Git 快照复查差异；默认仍加载当前扩展。
