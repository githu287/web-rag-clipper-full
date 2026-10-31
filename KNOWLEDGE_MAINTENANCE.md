# 知识库数据巡检与孤立向量清理

`scripts/audit_knowledge_base.py` 是操作者在后端机器运行的维护工具。读取当前 `.env` 的 MySQL/Milvus 配置，使用现有 Python 依赖，不调用收费模型。没有新增公开管理 API，不会自动运行或定时删除数据。

## 只读巡检

在仓库根目录执行，输出文件使用新的名称：

```powershell
.venv/Scripts/python.exe scripts/audit_knowledge_base.py scan --output evaluation/private/kb-scan-001.json
```

检查范围：

- 向量对应的 `page_id` 是否在 MySQL 文档表中存在；不存在的列为 `orphans`。
- `SUCCESS` 文档是否缺少向量、实际数量是否与 `chunk_count` 一致、分块序号是否从0连续。
- 非成功文档是否仍有向量，主键是否符合其 `page_id` 和分块序号。
- 扫描前后文档快照是否变化，是否存在排队、运行或删除中的操作。

报告仅含编号、状态、数量和内容校验摘要，不导出原文、向量或凭证。向量使用强一致迭代读取，不以 `count(*)` 或物理存储统计代替逐行核对，也不受单次查询窗口限制。默认最多扫描100000条，可用 `--max-rows` 调整至最高1000000；达到上限或出现异常时中止，不生成不完整的可清理报告。

`stable: false` 表示观察到活动任务或 SQL 快照变化，报告可供排查，不能用于清理。跨 MySQL/Milvus 的扫描不是分布式原子快照；活跃使用期间应在空闲时重新扫描，确认异常是否持续。`stable: true` 仅表示本次检查未观察到变化。退出码0表示操作完成，仍需看报告中的异常数量；退出码1表示操作失败。

## 按清单清理

先查看报告并预演，以下命令不删除、不创建备份：

```powershell
.venv/Scripts/python.exe scripts/audit_knowledge_base.py cleanup --report evaluation/private/kb-scan-001.json
```

真实清理需要安排维护窗口：等待在途任务完成，暂停本项目 API、入库 Worker、视频 Worker及其他写入程序。`--maintenance-window` 是操作者已完成暂停的声明，工具本身不停止服务。确认环境后执行：

```powershell
.venv/Scripts/python.exe scripts/audit_knowledge_base.py cleanup --report evaluation/private/kb-scan-001.json --backup-dir evaluation/private/kb-backup-001 --apply --maintenance-window
```

工具会重新核对数据库、集合和维度，确认清单中的文档仍不存在、向量内容与清单一致，检查没有活动任务；随后导出完整的原文和1024维向量，落盘并回读校验。只按清单中的确切主键分批删除，每批前复查 SQL 和完整向量，删除后强一致读取确认不可见。新增孤立向量不会自动加入旧清单。已有文档的数量异常、非成功文档向量等仅报告，不在自动清理范围。

清单过期、字段被篡改、记录变化、备份异常或删除异常都会停止。备份目录必须不存在，报告与文件不覆盖旧记录。没有候选时不执行无过滤查询或删除。当前完整备份会载入内存，大规模清理前应根据主机内存安排容量；普通只读扫描不读取 embedding。

备份包含：

- `scan.json`：本次输入报告。
- `vectors.json`：完整可重新写入的记录，包括主键、文档编号、分块序号、原文和 embedding；SDK的 `float32` 值转为可精确表示它的 JSON 数值。
- `manifest.json`：目标身份、记录数、报告及备份的 SHA-256。
- `cleanup-result.json`：完成或中断状态和已确认删除的主键；进程被强制终止时可能尚无回执，须按备份重新核对。

备份含知识库内容，应保留在本机私有目录中。上述 `evaluation/private/` 与本机 `.venv/runtime/` 都被 Git 忽略。恢复前应核对 manifest 和目标集合，先在独立测试集合验证；`vectors.json` 中的列表可由同版本 `MilvusClient.upsert(data=rows)` 写入兼容 Schema。直接恢复主集合会重新引入这些孤立向量，不应作为日常操作。工具没有自动恢复功能。

清理完成后重新执行 `scan` 写入新报告，恢复 API 和 Worker。应用删除与重试路径沿用原有行为，本工具提供核对与历史残留维护能力，不能替代数据库备份或源文档恢复。

## 2026-10-31 本机执行结果

上一轮发现的50条向量（文档18至57）与本次扫描的文档编号及数量一致；当前 SQL 无这些文档。首次实际导出因 SDK 返回 `numpy.float32` 被严格校验拦截，未删除数据，已补兼容转换和测试。随后在暂停服务期间备份、校验并删除50条，服务恢复；逐行复查向量与孤立向量均为0。

用备份的50条完整记录在随机命名的独立 Milvus 测试集合实际还原，原文、字段与每个向量值的摘要均一致，测试集合已删除。

真实 API、Redis Worker、MySQL、线上 Embedding/问答及 Milvus 验证结果如下。使用隔离临时 Workspace 和自制中文事实文本，原有空间配置不变：

| 阶段 | 文档数 | 可查询向量数 | 孤立/文档分块异常 |
|---|---:|---:|---|
| 多分块网页入库 | 1 | 8 | 0 / 0 |
| 文件上传后 | 2 | 9 | 0 / 0 |
| 同一规范化URL重剪并缩短为1块 | 2 | 2 | 0 / 0 |
| 故意错误编码的文件修复后重试 | 3 | 3 | 0 / 0 |
| 显式删除网页文档 | 2 | 2 | 0 / 0 |
| 删除测试空间后 | 0 | 0 | 0 / 0 |

真实检索与问答返回测试事实527及正确文档来源，失败文件第二次尝试成功。最终1098项后端测试、119个子测试通过，含新增25项维护回归：备份完整性、SDK精度、SQL变化、活动任务、主键与文档编号篡改、备份后向量变化、错误和部分中断等。两条既有依赖弃用警告不变。

本机私有证据和备份位于 `.venv/runtime/knowledge-maintenance-20261031/`：`initial-scan.json`、`backup-50/`、`after-cleanup.json`、`backup-restore-test.json`、`live-acceptance.json`、`final-scan.json`。完整备份与第三方历史正文未提交 Git。此次没有追溯恢复已丢失的 SQL 归属，也不声称已证明50条旧记录最初产生的全部原因；当前实际删除、重剪缩短与失败重试未再产生残留。

首轮Windows CI曾在既有视频取消测试失败：取消信号在回环服务收到请求后才产生，但0.2秒总预算可能先被SDK/证书初始化耗尽，实际返回504而非预期409。取消测试现使用独立预算，从服务收到请求产生取消信号后衡量响应速度；短预算的静默超时测试保持原有要求。生产超时和取消逻辑未因此放宽，CI失败记录保留。
