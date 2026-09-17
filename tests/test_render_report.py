"""Unit tests for evaluation.render_report.

独立运行：pytest tests/test_render_report.py
使用 mock 的 run_baseline JSON 字典（含 environment_snapshot / cases /
failure_cases 等全部必需字段）验证 Markdown 渲染结果。
"""

from __future__ import annotations

import json
from typing import Any

from evaluation.render_report import (
    load_report,
    render_markdown,
    render_report_file,
)


def _case(**overrides: Any) -> dict[str, Any]:
    """构造一条与 baseline.CaseResult asdict() 形状一致的 case 记录。"""
    base: dict[str, Any] = {
        "case_id": "sg001",
        "dataset": "rag_eval.jsonl",
        "category": "simple_fact",
        "status_code": 200,
        "latency_ms": 12.3,
        "retrieved_document_ids": [],
        "retrieved_chunk_ids": [],
        "scores": [],
        "hit": None,
        "recall": None,
        "precision": None,
        "reciprocal_rank": None,
        "ndcg": None,
        "leakage_count": 0,
        "error": None,
        "mode": "all",
        "test_layer": "",
        "is_answerable": True,
        "abstention_class": "",
        "max_score": None,
        "query": "",
        "expected": {"gold_document_ids": [], "gold_chunk_ids": []},
    }
    base.update(overrides)
    return base


def _sample_report() -> dict[str, Any]:
    """构造覆盖全部章节的 mock JSON 报告字典。"""
    success_case = _case(
        case_id="sg001",
        dataset="rag_eval.jsonl",
        category="simple_fact",
        retrieved_document_ids=[98],
        retrieved_chunk_ids=["98_0"],
        hit=1.0,
        recall=1.0,
        precision=1.0,
        reciprocal_rank=1.0,
        ndcg=1.0,
        query="StateGraph 构造时必须接收什么参数？",
        expected={"gold_document_ids": [98], "gold_chunk_ids": ["98_0"]},
    )
    leak_case = _case(
        case_id="isoL1_01",
        dataset="isolation_eval.jsonl",
        category="plugin_isolation_L1_docid_belonging",
        test_layer="L1_Retrieval",
        retrieved_document_ids=[118],
        retrieved_chunk_ids=["118_0"],
        hit=0.0,
        recall=0.0,
        precision=0.0,
        reciprocal_rank=0.0,
        leakage_count=3,
        query="StateGraph 的 reducer 如何声明？",
        expected={"gold_document_ids": [98], "gold_chunk_ids": []},
    )
    failed_case = _case(
        case_id="fail01",
        dataset="rag_eval.jsonl",
        category="simple_fact",
        retrieved_document_ids=[501, 502],
        retrieved_chunk_ids=["501_0", "502_1"],
        hit=0.0,
        recall=0.0,
        precision=0.0,
        reciprocal_rank=0.0,
        query="一个注定检索不到答案的问题",
        expected={"gold_document_ids": [101], "gold_chunk_ids": ["101_0"]},
    )

    return {
        "schema_version": 1,
        "environment_snapshot": {
            "timestamp": "2026-09-07T10:30:00+08:00",
            "python_version": "3.11.9",
            "platform": "Windows-11-10.0.22631",
            "dataset_name": "rag_eval.jsonl, isolation_eval.jsonl",
            "top_k": 5,
            "warmup_runs": 3,
        },
        "summary": {
            "top_k": 5,
            "case_count": 3,
            "successful_case_count": 2,
            "error_rate": 0.0,
            "hit_rate": 0.9,
            "recall_at_k": 0.8,
            "precision_at_k": 0.45,
            "mrr": 0.87,
            "ndcg_at_k": 0.82,
            "isolation_leakage_count": 3,
            "latency_ms_mean": 100.0,
            "latency_ms_p95": 150.0,
        },
        "breakdowns": {
            "by_dataset": {},
            "by_category": {
                "simple_fact": {
                    "top_k": 5,
                    "case_count": 2,
                    "successful_case_count": 2,
                    "error_rate": 0.0,
                    "hit_rate": 0.9,
                    "recall_at_k": 0.8,
                    "precision_at_k": 0.45,
                    "mrr": 0.87,
                    "ndcg_at_k": 0.82,
                    "isolation_leakage_count": 0,
                    "latency_ms_mean": 100.0,
                    "latency_ms_p95": 150.0,
                },
                "plugin_isolation_L1_docid_belonging": {
                    "top_k": 5,
                    "case_count": 1,
                    "successful_case_count": 1,
                    "error_rate": 0.0,
                    "hit_rate": 1.0,
                    "recall_at_k": 1.0,
                    "precision_at_k": 1.0,
                    "mrr": 1.0,
                    "ndcg_at_k": 1.0,
                    "isolation_leakage_count": 3,
                    "latency_ms_mean": 10.0,
                    "latency_ms_p95": 10.0,
                },
            },
            "by_mode": {},
            "by_test_layer": {},
        },
        "score_threshold_analysis": {},
        "cases": [success_case, leak_case, failed_case],
        # run_baseline 输出不含该字段；这里显式提供，同时覆盖
        # “顶层 failure_cases 优先”与渲染前 5 限制。
        "failure_cases": [failed_case, leak_case],
    }


def test_render_markdown_includes_environment_snapshot() -> None:
    md = render_markdown(_sample_report())
    assert "# Retrieval Evaluation Report" in md
    assert "## 1. Environment Snapshot" in md
    assert "3.11.9" in md
    assert "Windows-11-10.0.22631" in md
    assert "rag_eval.jsonl, isolation_eval.jsonl" in md


def test_render_markdown_includes_overall_metrics() -> None:
    md = render_markdown(_sample_report())
    assert "## 2. Overall Metrics" in md
    assert "Hit@K" in md
    assert "Recall@K" in md
    assert "Precision@K" in md
    assert "MRR" in md
    # 0.9 -> 90.0%，0.45 -> 45.0%，0.87 -> 0.8700
    assert "90.0%" in md
    assert "45.0%" in md
    assert "0.8700" in md


def test_render_markdown_includes_category_metrics() -> None:
    md = render_markdown(_sample_report())
    assert "## 3. Category Metrics" in md
    assert "simple_fact" in md
    assert "plugin_isolation_L1_docid_belonging" in md


def test_render_markdown_includes_isolation_leakage() -> None:
    md = render_markdown(_sample_report())
    assert "## 4. Isolation Leakage" in md
    assert "L1_Retrieval" in md
    assert "Leaked Cases" in md
    assert "leakage=3" in md


def test_render_markdown_includes_failure_cases() -> None:
    md = render_markdown(_sample_report())
    assert "## 5. Failure Cases" in md
    assert "一个注定检索不到答案的问题" in md
    assert "documents=[101]" in md  # expected gold_document_ids
    assert "documents=[501, 502]" in md  # retrieved document ids


def test_render_markdown_includes_raw_reference() -> None:
    md = render_markdown(_sample_report(), source_path="evaluation/reports/baseline.json")
    assert "## 6. Raw Result Reference" in md
    assert "baseline.json" in md


def test_render_report_file_writes_markdown(tmp_path) -> None:
    """端到端：写 mock JSON 到临时文件 → 渲染出 .md 且章节完整。"""
    raw = tmp_path / "retrieval-baseline.json"
    raw.write_text(
        json.dumps(_sample_report(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    output = tmp_path / "retrieval-baseline.md"

    result = render_report_file(raw, output)

    assert result == output
    assert output.is_file()
    md = output.read_text(encoding="utf-8")
    for expected in [
        "# Retrieval Evaluation Report",
        "## 2. Overall Metrics",
        "## 4. Isolation Leakage",
        "## 5. Failure Cases",
        "## 6. Raw Result Reference",
        "retrieval-baseline.json",
    ]:
        assert expected in md


def test_load_report_returns_parsed_dict(tmp_path) -> None:
    raw = tmp_path / "report.json"
    raw.write_text(json.dumps(_sample_report()), encoding="utf-8")
    data = load_report(raw)
    assert data["environment_snapshot"]["python_version"] == "3.11.9"
    assert data["summary"]["mrr"] == 0.87
