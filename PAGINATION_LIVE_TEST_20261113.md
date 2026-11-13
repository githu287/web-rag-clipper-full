# 中文分页文章与实际侧栏续采（0.18.3）

2026-11-13，扩展0.18.3、提取器1.7.3。隔离无头Chrome访问真实公开网页，使用实际扩展侧栏及生产分页预算；未读取用户凭证、写入知识库或调用模型。原生侧栏容器仍未人工操作，测试打开的是同一扩展页面。

## 修复

游民星空部分图文攻略使用 `.Mid2L_con`，现有通用候选未识别该容器。回退Readability时，短页或图片页可能选中页脚：公开图集首轮只提取802字符的版权/联系链接，后续页因内容相同被停止；安装教程第4/5页也漏掉正文，标题多数缺失。

增加仅作用于游民星空域名、`/handbook/YYYYMM/数字[_页码].shtml`路径的正文规则：选择可见正文容器，补充容器外可见H1，清理已观察到的分页目录、投票、编辑信息、推广按钮和广告槽。其他域名、其他路径或隐藏/缺失容器继续使用通用提取。普通整页、选区和指定区域沿用其明确选择范围。

图片地址池原有33张上限保持不变。现在单页、滚动、分页和框架合并遇到额外候选时，都添加明确提醒，避免把未处理图片当作已采集完毕。仍需开启图片文字识别才会执行OCR，每轮最多8张；达到候选池上限后的图片建议分区域另行剪藏。

## 真实网页结果

| 场景 | 本轮观察 |
|---|---|
| [PC安装运行教程，从首页开始](https://www.gamersky.com/handbook/201511/680884.shtml) | 修复前前5页参考正文块59/69，标题仅1/5；修复后69/69、标题5/5。图片候选池33个来源与参考匹配 |
| 同一教程实际侧栏续采 | 首次5页，续采至10页、再至15页；123/123个参考正文块、15/15个标题通过。累计预览29796字符（含测试编辑）；每页来源按顺序仅出现一次，保留原标题/正文编辑，原网页位置不变，临时页清理完成 |
| 首次续采放弃后重试 | 保留原5页与编辑，重试仍从第6页开始；确认才推进进度。实际侧栏执行，未替换网页或分页响应 |
| [从网站第6页开始](https://www.gamersky.com/handbook/201511/680884_6.shtml) | 初次读取第6–10页，续采至第15页，再保留至第17页，共12页；60/60个参考正文块通过。第18页与已读页重复时停止，未补读第1–5页；来源绑定仍是第6页 |
| [图集正文](https://www.gamersky.com/handbook/201803/1019303.shtml) | 修复前仅1页页脚；修复后可合并5页，标题及所选可见文字通过。前5页参考65个图片来源，保留前33个并明确提醒候选上限，结果标记为“通过但有图片上限” |
| 图集实际侧栏续采 | 5→10页、放弃再试和确认追加通过，10个标题及来源顺序正确；OCR关闭，未声称取得图片中的文字 |
| [旧文章的HTTP下一页](https://www.gamersky.com/handbook/201007/162084.shtml) | 当前页18/18个参考正文块、3个图片来源通过；下一页使用HTTP，当前页为HTTPS，按现有同源规则停止，不自动改写协议 |

**原站内容缺失边界：** 独立打开安装教程第17和18页，正文都只显示“丢失”。从第6页开始的第二轮续采没有达到最初预期的15个采集页，而是在12页后以`repeated_content`停止。原始`needs_review`报告保留；独立核对网站DOM后，新增该边界的明确预期并重新运行通过。第17页缺失提示仍在已接受预览中，并有短内容提醒；不能把这12页当作完整教程恢复。没有放宽重复检测来跳过缺失页。

## 参考方法与限制

正文参考来自重新打开的实际页面中指定正文容器，排除已观察到的目录/控件，比较标题和不少于30字符的可见正文块。该标准不覆盖所有短标签，也不是逐字人工标注或全站完整率；图片页后续很多页面没有达到30字符的段落，主要核对标题、来源与候选地址。

图片审计同时记录同一DOM图片公开暴露的懒加载地址及实际加载地址。浏览器可能将已加载的HTTP图片升级为HTTPS，重新打开的参考页面又处于另一加载阶段；只接受该图片DOM中实际出现的地址选择，不把任意HTTP/HTTPS地址视作相同。初版严格地址比较因此产生两条`needs_review`记录，保留原结果；完善参考记录后复跑通过。生产分页同源规则和图片读取规则没有因此改变。

实际侧栏流程关闭OCR，因此报告中的图片匹配项不参与侧栏文字验收。地址池上限另由直接扩展采集检查，输出每页发现/匹配数、累计候选数及上限提醒；没有将33/65伪装为全图片采集成功。本轮未对新图集运行真实OCR、Embedding或问答，也没有恢复之前人民网三个样本的访问/证书问题。

此前另探查3DM页面作为候选，发现正文外还有导航和内嵌内容；该站未在本轮做专门适配，探查结果不计入新增站点通过结论。

## 回归与工具

- 普通剪藏浏览器12组通过，原创站点DOM样本由13增至18份：短正文/图片/表格、目录清理、33图上限，以及错误域名、路径、隐藏容器反例。
- 滚动浏览器13组、分页浏览器16组、续采浏览器11组通过，共52组；Node全部23组、JavaScript语法与公开数据150条格式检查通过。
- Node补充跨框架、跨页图片候选上限及提醒。后端、数据库和OCR模型未修改。
- `audit_pagination_sites.py --continue-rounds`运行实际侧栏核对、放弃、重试、确认，检查新增来源顺序、编辑保留和临时页清理；在线审计不以站点可访问性决定CI成败。原本地回归继续进入CI，远端结果需推送后查看。

```powershell
.venv/Scripts/python.exe scripts/audit_pagination_sites.py --help
.venv/Scripts/python.exe scripts/audit_pagination_sites.py --only gamersky-pc-faq gamersky-gallery gamersky-http-next-boundary
.venv/Scripts/python.exe scripts/audit_pagination_sites.py --only gamersky-pc-faq gamersky-pc-faq-middle --continue-rounds 2
.venv/Scripts/python.exe scripts/audit_pagination_sites.py --only gamersky-gallery --continue-rounds 1
.venv/Scripts/python.exe scripts/check.py extension datasets
.venv/Scripts/python.exe scripts/check_capture_browser.py
.venv/Scripts/python.exe scripts/check_scroll_capture_browser.py
.venv/Scripts/python.exe scripts/check_pagination_browser.py
.venv/Scripts/python.exe scripts/check_capture_continuation_browser.py
```

原始证据在Git忽略的 `.venv/runtime/pagination-live-20261113/`。旧版对照使用从已提交`a605380`导出的独立扩展副本；网页全文、截图及运行配置不提交仓库。重新加载扩展即可使用，无需重启后端或迁移数据库。PDF与Groq403继续暂缓，共享服务器部署和旧数据迁移仍未执行。
