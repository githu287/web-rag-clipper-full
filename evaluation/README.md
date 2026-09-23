# Retrieval Evaluation Baseline

本目录提供独立于后端业务代码的 Retrieval 基线工具，用于测量检索质量、延迟和 Workspace 隔离。原始 Gold Dataset 保持不变；真实 ID、凭证和运行报告只能写入 Git 已忽略的运行时目录。

## 数据组成

| 数据集 | 数量 | 用途 |
|---|---:|---|
| `rag_eval.jsonl` | 70 | 主 Retrieval/RAG 问题 |
| `negative_eval.jsonl` | 10 | 无答案与拒答场景 |
| `isolation_eval.jsonl` | 70 | Workspace 隔离与越权探测 |

源语料包含两个 Workspace、共 40 篇文档。占位符、主题清单和人工复核状态见 `datasets/DATASET_MANIFEST.md`。

## 运行前提

- API、MySQL 和 Milvus 已启动，目标文档均为 `SUCCESS`。
- 两个评测 Workspace 使用相同的 Embedding 配置；切换模型后必须重新入库并重新对齐。
- API 当前运行在 `http://localhost:18000`。`run_baseline.py` 的历史默认值仍是 8000，因此下面命令显式传入 `--base-url`。
- `evaluation/private/`、`aligned*/` 和 `reports/` 已被 `.gitignore` 排除。

## 1. 校验原始数据

```powershell
python -m evaluation.validate_datasets `
  evaluation/datasets/rag_eval.jsonl `
  evaluation/datasets/negative_eval.jsonl `
  evaluation/datasets/isolation_eval.jsonl `
  --allow-placeholders
```

## 2. 对齐占位符

准备 `dataset_placeholder_mapping.json`，键为完整占位符，例如：

```json
{
  "<PLUGIN_A_PID>": "actual-plugin-id",
  "<DOC_A01>": 101,
  "<CHUNK_A01_1>": "101_0"
}
```

生成独立 aligned 数据集：

```powershell
python -m evaluation.align_datasets `
  --mapping evaluation/private/dataset_placeholder_mapping.json `
  --output-dir evaluation/aligned `
  evaluation/datasets/rag_eval.jsonl `
  evaluation/datasets/negative_eval.jsonl `
  evaluation/datasets/isolation_eval.jsonl
```

如果运行时真实切块数与 Part-A 的 Chunk 占位符不一致，在人工重标
`gold_chunk_ids` 前不得伪造 Chunk ID。可先生成文档级基线数据：

```powershell
python -m evaluation.align_datasets `
  --mapping evaluation/private/dataset_placeholder_mapping.json `
  --output-dir evaluation/aligned-document `
  --document-only `
  evaluation/datasets/rag_eval.jsonl `
  evaluation/datasets/negative_eval.jsonl `
  evaluation/datasets/isolation_eval.jsonl
```

该模式保留真实 Plugin/Document Gold，清空 `gold_chunk_ids` 与基于 Chunk 的
`relevance_grading`；因此报告是文档级临时基线，不能冒充 Chunk 级评估。

## 3. 校验运行时对齐

对齐完成后，应先核对 MySQL 归属、Document 状态和 Milvus Chunk ID：

```powershell
python -m evaluation.validate_runtime_alignment `
  evaluation/aligned/rag_eval.jsonl `
  evaluation/aligned/negative_eval.jsonl `
  evaluation/aligned/isolation_eval.jsonl
```

只有校验通过的完整 Chunk Gold 数据才适合计算 Chunk 级 Recall、Precision、MRR 与 nDCG。`aligned-document/` 只能作为文档级临时基线。

## 4. 运行 Retrieval Baseline

凭证文件只保存在本地，格式为：

```json
{
  "actual-plugin-id": {"plugin_secret": "local-secret"}
}
```

```powershell
python -m evaluation.run_baseline `
  --base-url http://localhost:18000 `
  --credentials evaluation/private/credentials.json `
  --dataset evaluation/aligned/rag_eval.jsonl `
  --dataset evaluation/aligned/negative_eval.jsonl `
  --dataset evaluation/aligned/isolation_eval.jsonl `
  --output evaluation/reports/retrieval-baseline.json `
  --top-k 5
```

JSON 结果包含 Hit@K、Recall@K、Precision@K、MRR、nDCG@K、隔离泄漏数、平均/P95 检索延迟和错误率，并保留逐样本 raw result 元数据。

## 5. 生成 Markdown 报告

```powershell
python -m evaluation.render_report `
  --input evaluation/reports/retrieval-baseline.json `
  --output evaluation/reports/retrieval-baseline.md
```

## 结果解释

- `Hit@K`：前 K 条是否至少命中一个 Gold。
- `Recall@K`：Gold 被召回的比例。
- `Precision@K`：前 K 条中相关结果的比例。
- `MRR`：第一个相关结果排名的倒数。
- `nDCG@K`：仅在 relevance grading 覆盖充分时有意义。
- Isolation Leakage：必须为 0；任何跨 Workspace 命中都应优先处理。

不要把真实 Plugin Secret、对齐后的内部 ID 或包含私有查询内容的报告提交到仓库。
