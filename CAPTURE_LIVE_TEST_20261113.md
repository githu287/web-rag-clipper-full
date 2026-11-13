# 网页剪藏真实续采验收（0.18.2）

2026-11-13，隔离无头Chrome加载真实扩展，访问实际公开网页，使用生产滚动限额。基础在线提取检查开始于0.18.1，下面的界面修正及最终续采复跑使用0.18.2。没有读取用户身份或模型Key，没有向用户数据库写入内容，也没有云端模型调用。

## 实际修正

大型README中，全文已经存在于DOM，首次提取就能取得正文，但滚动到20次上限仍会停止。继续滚动两轮均未发现新增文字。原界面只显示空白的“新增内容”，难以判断结果；现在会明确提示“本轮没有新增文字”，并显示确认后累计滚动次数、停止原因、分页/图片状态。

空白新增内容对应“确认更新采集进度”，只接受新游标，原正文逐字保持不变；填写新增文字后切回“确认追加到当前预览”。提示随新增内容编辑更新。取消或放弃仍保留原游标；接受或放弃后清除本轮新增文本，身份变化时同时清理提示。原有单轮预算、50万字符和30页累计上限不变，需重新加载扩展，无后端或数据库变更。

## 真实文字页面

| 页面 | 参考正文块 | 原始代码块 | 观察 |
|---|---|---|---|
| [Mozilla Readability README](https://github.com/mozilla/readability) | 42/42 | 6/6 | 开启滚动提取通过 |
| [yt-dlp大型README](https://github.com/yt-dlp/yt-dlp) | 661/661 | 47/47 | 提取176317字符，侧栏按生产20次/轮限制再续采两轮，共累计60次；两轮新增均为0，原编辑和正文保留，无重复追加 |
| [Vue响应式基础](https://cn.vuejs.org/guide/essentials/reactivity-fundamentals.html) | 64/64 | 27/27 | 实际侧栏滚动到当前底部后停止，没有误跟进“下一章”；正文和代码通过 |
| [微信文字文章](https://mp.weixin.qq.com/s/7W8MjSxdnqCAwfgOPQRxjQ) | 16/16 | 0/0 | 普通提取通过，2719字符 |
| [博客园装饰器文章](https://www.cnblogs.com/tobyqin/p/python-decorator.html) | 76/76 | 29/29 | 普通提取通过，12924字符 |

合计859/859个参考可见正文块、109/109个代码块通过。参考来自当次渲染页面中选定的正文区域；Markdown链接先还原为显示文字，代码保留独立源码比较。不是逐字人工准确率、噪声率或未加载内容覆盖率。大型README的正文原本已加载，不能替代虚拟列表测试。

初版新侧栏审计器没有还原Markdown链接显示文字，出现两条正文块比较误报；原结果保留在运行目录。补齐与现有普通审计一致的比较规则后，完整浏览器复跑通过。这一修正发生在审计脚本中。

## 真实图片及手动恢复

[微信图片文章](https://mp.weixin.qq.com/s/v8g0jI-c49RsihsaPBgeRQ)的普通DOM只有198字符，本轮普通文字审计标记为`reference_short`，不当作图片内容已提取。图片使用实际网络地址读取，交给隔离API中的生产OCR路由及真实本地CPU模型。认证服务使用临时固定身份，数据库服务不启用。

- 无故障注入的首次识别：5张候选，4张有文字，1张未发现文字，0张失败；合并预览5200字符。
- 恢复场景：另一次运行在第一次OCR请求前明确注入一次503，不执行该次模型；初始预览只有198字符，状态为1张失败、4张未处理。
- 用户手动重试后，5张实际图片均进入真实OCR流程，新增5002字符；最终4张有文字、1张无文字、0张失败/未处理。总API调用6次，其中1次是注入故障，5次实际识别。
- 新文字先进入待合并区域，确认前展示最终图片统计；确认后保留原标题编辑和原正文。普通API与模型响应未用固定文字替代。

没有逐字人工标注准确率，没有重做入库和问答链路。临时认证意味着这不是用户账户的认证验收；注入503仅验证可控失败后的实际恢复，不能说明线上服务发生过该故障。已成功图片不重复识别的混合状态另由本轮11组本地续采回归覆盖。

## 分页访问边界

原有三个人民网分页样本的HTTP导航本轮均返回`ERR_BLOCKED_BY_CLIENT`。同地址改为HTTPS后均返回`ERR_CERT_COMMON_NAME_INVALID`；保留两种结果，没有关闭证书检查或修改站点权限规则。地址见 `extension/tests/pagination-sites.json`。

Vue的下一章负例通过，但**本轮未完成真实多页文章的继续读取验收**。不能用旧报告或本地分页成功代替这一结论。分页审计工具现在额外保存错误信息，便于区分传输错误和提取问题。

## 回归与复跑

本轮新增/续采本地浏览器11组通过，普通剪藏浏览器12组通过（含13份站点DOM模式样本），全部23组Node回归及JavaScript语法检查通过。公开数据150条格式通过。新本地检查覆盖空白新增内容的进度接受、编辑后操作名称变化、待合并分页/图片统计、取消/放弃、来源和工作区隔离。

新增 `scripts/audit_capture_continuation_sites.py` 使用真实侧栏执行提取、编辑、续采和确认，生产限额不下调，原始页面不被替换。实测结果和截图默认写到Git忽略的运行目录；在线访问不稳定，不加入CI成功条件。本地回归继续由既有Chromium CI运行，推送后的远端结果另行查看。

```powershell
.venv/Scripts/python.exe scripts/audit_capture_continuation_sites.py --help
.venv/Scripts/python.exe scripts/audit_capture_continuation_sites.py --only github-yt-dlp vue-guide
.venv/Scripts/python.exe scripts/audit_capture_continuation_sites.py --only wechat-science --ocr
.venv/Scripts/python.exe scripts/audit_capture_continuation_sites.py --only wechat-science --ocr --fail-first-ocr
.venv/Scripts/python.exe scripts/check_capture_continuation_browser.py
.venv/Scripts/python.exe scripts/check_capture_browser.py
.venv/Scripts/python.exe scripts/check.py extension datasets
```

需要已安装Playwright、Chrome；OCR选项另需项目的可选本地OCR依赖。底层可选依赖仍有既有`pkg_resources`弃用提醒，本轮未改变依赖版本。证据在 `.venv/runtime/capture-live-20261113/`：基础在线检查、HTTP/HTTPS访问记录、正常OCR、恢复OCR、修正前后侧栏审计及本地回归分目录保留，不提交网页全文或图片。

后续继续扩展可访问的中文分页、懒加载及虚拟列表真实样本。PDF、Groq403和完整新语音链路按用户要求继续暂缓；共享服务器部署与旧数据迁移未在本轮执行。
