# Retrieval Evaluation Baseline

本目录提供独立于 Backend 业务代码的 Retrieval 基线工具。原始 Gold Dataset 保持不变；运行时对齐结果应写入未提交的单独目录。

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

## 3. 运行 Retrieval Baseline

凭证文件只保存在本地，格式为：

```json
{
  "actual-plugin-id": {"plugin_secret": "local-secret"}
}
```

```powershell
python -m evaluation.run_baseline `
  --credentials evaluation/private/credentials.json `
  --dataset evaluation/aligned/rag_eval.jsonl `
  --dataset evaluation/aligned/negative_eval.jsonl `
  --dataset evaluation/aligned/isolation_eval.jsonl `
  --output evaluation/reports/retrieval-baseline.json `
  --top-k 5
```

报告包含 Hit@K、Recall@K、Precision@K、MRR、nDCG@K、隔离泄漏数、平均/P95 检索延迟和错误率，并保留逐样本 raw result 元数据。

`evaluation/private/`、`evaluation/aligned/` 和 `evaluation/reports/` 不应提交真实 Plugin Secret。
