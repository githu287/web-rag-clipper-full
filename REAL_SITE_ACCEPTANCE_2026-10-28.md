# 真实网页与入库问答验收（0.11.1）

2026-10-28，Windows、隔离无头 Chrome、实际加载的扩展。普通网页与分页审计直接访问公开网站；端到端验收使用运行中的 FastAPI、MySQL、Redis、单 Worker、Milvus、真实 Embedding/问答服务和本地 PP-OCRv4，未替换网页、OCR、后端或模型响应。

## 普通网页与滚动

重新运行 `extension/tests/site-samples.json` 的 13 个公开样本：

| 样本 | 结果 |
|---|---|
| 微信文字、掘金、博客园、GitHub README、Vue、MDN、新华网、政府公告、发改委，共 9 个 | 标题和本轮参考可见正文块检查通过；312/312 块保留 |
| 以上技术文章的代码 | 78/78 个参考代码块通过源码比较；参考检查补充了开放 Shadow DOM，涵盖 MDN 的 8 个示例，最终 0.11.1 全样本复跑通过 |
| 微信图片文章 | 普通 DOM 只有少量文字；开启真实 OCR 后完成下述入库和图片事实问答 |
| 知乎文章、知乎回答、CSDN，共 3 个 | 返回 HTTP 403，扩展拒绝生成受限页正文；未验证登录后的内容 |

GitHub README 与 Vue 另开启“加载更多后提取”复跑，正文块和代码检查通过，耗时约 11.3/17.2 秒。这些度量比较本次可见 DOM 中选定的正文块和代码，不是逐字人工标注准确率，也不代表整站覆盖率。MDN 还包含开放组件和内嵌演示，输出比 light DOM 参考范围更长；不能用长度推断没有噪声或遗漏。

原始样本 URL 见 `extension/tests/site-samples.json`，此前问题背景见 [普通网页实测](SITE_AUDIT_2026-10-28.md)。

## 真实分页与版本对照

| 实际公开页面 | 本轮结果 |
|---|---|
| [人民网北京三页文章](http://bj.people.com.cn/n2/2026/0125/c14540-41482429.html) | 顺序合并 3 页，1549 字符；参考正文块 6/6、主图地址 3/3，各页标题及来源保留 |
| [人民网图片文章，从第 2 页开始](http://pic.people.com.cn/n1/2026/0308/c1016-40677491-2.html) | 当前页 398 字符，参考块 2/2、主图 1/1；下一页链接被网站隐藏，JS 图集控制未跟进，不能视为完整 11 页图集 |
| [人民网历史图集，从第 2 页开始](http://lianghui.people.com.cn/2016npc/n1/2016/0305/c402194-28173982-2.html) | 当前页 3368 字符，参考块 42/42、标题及主图 1/1；隐藏下一页未跟进，不能视为完整 12 页图集 |
| [Vue 响应式基础](https://cn.vuejs.org/guide/essentials/reactivity-fundamentals.html) | 负例通过：保留当前章，参考块 64/64，没有把“下一章”合并进来 |

四个场景均保持原网页 URL、阅读位置及文档绑定，临时标签页 session 记录清理完成。北京文章第 3 页后的“下一页”调用 JavaScript 广告函数，扩展拒绝跟进并显示分页不完整提醒；没有把广告当成第 4 页。

这些人民网 URL 的 HTTPS 访问在本机 Chrome 报 `ERR_CERT_COMMON_NAME_INVALID`，因此另测网站公开可用的 HTTP 地址；没有关闭证书检查。HTTPS 和 HTTP 的结果不能互相替代。

用 `git archive` 导出已提交的 `697b9e7`（0.11.0），在另一个隔离配置中运行同一审计作对照：北京文章旧版只有 1 页、438 字符，误判后续标题中的 `【2】` 为不同文章；历史图集旧版缺失正文容器外的标题和主图。0.11.1 修复这两类问题，并在智能正文 OCR 候选中排除已知尺寸不超过 48×48 的装饰小图。选区、指定区域和整页模式仍保留用户明确选择的小图。

## 实际入库、OCR 与问答

通过真实扩展页面 UI 注册临时 Workspace，在内存中读取已有配置并通过生产接口验证、保存到该临时空间，原空间配置不变。随后完成：

1. 北京文章通过实际预览合并三页并提交，Worker 完成切块、真实 Embedding 和 Milvus 入库。
2. 指定文档检索找到第 3 页来源，真实问答正确返回第三页 URL，sources 均归属该文档。
3. 再次剪藏同一 URL 更新原文档，没有生成重复文档。
4. [微信图片文章](https://mp.weixin.qq.com/s/v8g0jI-c49RsihsaPBgeRQ) 经扩展读取真实图片、生产 OCR API 和本地模型处理：5 个候选，4 张识别出文字，0 张失败，另 1 张未识别出文字；合并预览共 5200 字符，显示需核对识别结果的提醒。
5. OCR 文本经真实 Worker 向量化入库。实际聊天 UI 提问：“‘2026科学跨年之夜’案例的实施单位是什么？”回答“北京科普发展与研究中心”，与原网页图片表格人工目视核对一致。该事实不在基础 DOM 正文中，返回来源归属 OCR 文档。
6. 错误 Workspace Secret 被实际 API 拒绝，返回 401。

随后删除测试文档及临时空间，并用强一致查询核对测试向量已清空。数据库测试前后均为原有 2 个 Workspace、0 个 Document、0 个 IngestJob；原空间状态和 `.env` 不变。该清理只针对本轮测试数据。

本轮实际 UI 在隔离无头 Chrome 的扩展页面中运行；原生 Side Panel 容器、工具栏展开/关闭以及用户日常浏览器配置尚未人工验收。网页数据随时间和访问环境变化，本报告只记录本次观察。

## 回归与复跑

离线检查：827 passed、117 subtests，13 组 Node 回归和 150 条评测数据格式检查通过。真实浏览器正文回归（含 12 份原创站点 DOM 模式）、OCR 预览及分页回归通过；分页新增 `【2】` 标题样本，OCR 新增装饰小图样本。后端与数据库结构未修改。

```powershell
.venv/Scripts/python.exe scripts/check.py all
.venv/Scripts/python.exe scripts/check_capture_browser.py
.venv/Scripts/python.exe scripts/check_ocr_browser.py
.venv/Scripts/python.exe scripts/check_pagination_browser.py
.venv/Scripts/python.exe scripts/audit_capture_sites.py
.venv/Scripts/python.exe scripts/audit_capture_sites.py --auto-scroll --only github-readme vue-guide
.venv/Scripts/python.exe scripts/audit_pagination_sites.py
```

在线审计不调用模型、不需要凭证，不纳入 CI 成败；支持用 `--extension-dir` 指定旧版扩展进行对照。完整入库/问答验收另使用本机已有模型配置，不能由上述只读审计命令代替。截图、原文、私有验收脚本及机器报告保留在忽略目录 `.venv/runtime/real-test-20261028/`，不将网页全文、凭证或模型配置提交到 Git。
