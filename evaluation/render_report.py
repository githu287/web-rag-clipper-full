"""Render a Markdown evaluation report from `evaluation.run_baseline` JSON output.

读取 run_baseline.py 产出的 JSON 报告，用纯 Python 字符串拼接渲染为结构化
Markdown。刻意不使用任何第三方模板引擎（无 Jinja2 等额外依赖）。

报告章节：
    1. Environment Snapshot    —— 运行环境快照
    2. Overall Metrics         —— Hit@K / Recall@K / Precision@K / MRR
    3. Category Metrics        —— 按类别细分的指标表
    4. Isolation Leakage       —— L1 / L2 / L5 隔离泄漏详情
    5. Failure Cases           —— 前 5 个失败案例（query / expected / retrieved）
    6. Raw Result Reference    —— 原始 JSON 文件路径

用法：
    python -m evaluation.render_report --input evaluation/reports/retrieval-baseline.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

REPORT_TITLE = "Retrieval Evaluation Report"
FAILURE_LIMIT = 5
LEAKAGE_LAYER_PREFIXES = ("L1", "L2", "L5")
_EMPTY = "—"


# --------------------------------------------------------------------------
# 基础格式化工具
# --------------------------------------------------------------------------
def _cell(value: Any) -> str:
    """把任意值转成安全的 Markdown 表格单元格文本。"""
    if value is None:
        return _EMPTY
    return str(value).replace("|", "\\|").replace("\n", " ").strip() or _EMPTY


def _number(value: Any, ndigits: int = 4) -> str:
    """格式化数值（None / 非数值回退为占位符）。"""
    if value is None:
        return _EMPTY
    if isinstance(value, (int, float)):
        return f"{float(value):.{ndigits}f}"
    return _cell(value)


def _percent(value: Any) -> str:
    """把 0~1 指标格式化为百分比（None / 非法值回退为占位符）。"""
    if value is None:
        return _EMPTY
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return _cell(value)


def _table(headers: list[str], rows: Iterable[Iterable[Any]]) -> str:
    """渲染一个 Markdown 表格（纯字符串拼接）。"""
    header_line = "| " + " | ".join(_cell(item) for item in headers) + " |"
    divider = "|" + "|".join(" --- " for _ in headers) + "|"
    body = ["| " + " | ".join(_cell(item) for item in row) + " |" for row in rows]
    return "\n".join([header_line, divider, *body])


def _heading(level: int, text: str) -> str:
    return f"{'#' * level} {text}"


def _compact_ids(ids: Iterable[Any], limit: int = 10) -> str:
    """把 id 列表压缩成可读字符串，超长截断。"""
    values = [str(item) for item in ids]
    if not values:
        return _EMPTY
    preview = ", ".join(values[:limit])
    if len(values) > limit:
        preview += ", …"
    return preview


# --------------------------------------------------------------------------
# 各章节渲染器
# --------------------------------------------------------------------------
def _render_snapshot(snapshot: dict[str, Any]) -> str:
    rows = [
        ["Timestamp", snapshot.get("timestamp")],
        ["Python Version", snapshot.get("python_version")],
        ["Platform", snapshot.get("platform")],
        ["Dataset Name", snapshot.get("dataset_name")],
        ["Top-K", snapshot.get("top_k")],
        ["Warmup Runs", snapshot.get("warmup_runs")],
    ]
    return "\n".join(
        [
            _heading(2, "1. Environment Snapshot"),
            _table(["Field", "Value"], rows),
        ]
    )


def _render_overall_metrics(summary: dict[str, Any]) -> str:
    rows = [
        ["Hit@K", _percent(summary.get("hit_rate"))],
        ["Recall@K", _percent(summary.get("recall_at_k"))],
        ["Precision@K", _percent(summary.get("precision_at_k"))],
        ["MRR", _number(summary.get("mrr"))],
        ["nDCG@K", _number(summary.get("ndcg_at_k"))],
        ["Total Cases", _cell(summary.get("case_count"))],
        ["Successful Cases", _cell(summary.get("successful_case_count"))],
        ["Error Rate", _percent(summary.get("error_rate"))],
    ]
    return "\n".join(
        [
            _heading(2, "2. Overall Metrics"),
            _table(["Metric", "Value"], rows),
        ]
    )


def _render_category_metrics(breakdowns: dict[str, Any]) -> str:
    by_category = breakdowns.get("by_category") or {}
    rows = [
        [
            category,
            stats.get("case_count"),
            _percent(stats.get("hit_rate")),
            _percent(stats.get("recall_at_k")),
            _percent(stats.get("precision_at_k")),
            _number(stats.get("mrr")),
            _percent(stats.get("error_rate")),
        ]
        for category, stats in sorted(by_category.items())
    ]
    return "\n".join(
        [
            _heading(2, "3. Category Metrics"),
            _table(
                ["Category", "Cases", "Hit@K", "Recall@K", "Precision@K", "MRR", "Error Rate"],
                rows,
            ),
        ]
    )


def _collect_leakage(cases: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """按 test_layer（L1/L2/L5 前缀）聚合泄漏 case。"""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        layer = str(case.get("test_layer") or "")
        if layer.startswith(LEAKAGE_LAYER_PREFIXES):
            buckets.setdefault(layer, []).append(case)
    return sorted(buckets.items())


def _render_isolation_leakage(cases: list[dict[str, Any]]) -> str:
    buckets = _collect_leakage(cases)
    if not buckets:
        return "\n".join(
            [
                _heading(2, "4. Isolation Leakage"),
                "No isolation cases found (test_layer prefix must be L1/L2/L5).",
            ]
        )

    rows = []
    leaked_cases: list[dict[str, Any]] = []
    for layer, items in buckets:
        leaked = [case for case in items if int(case.get("leakage_count") or 0) > 0]
        leaked_cases.extend(leaked)
        rows.append(
            [
                layer,
                len(items),
                len(leaked),
                sum(int(case.get("leakage_count") or 0) for case in items),
            ]
        )

    lines = [_heading(2, "4. Isolation Leakage")]
    lines.append(_table(["Layer", "Total Cases", "Leaking Cases", "Leak Entries"], rows))
    if leaked_cases:
        lines.append(_heading(3, "Leaked Cases"))
        lines.extend(
            f"- `{_cell(case.get('case_id'))}` [{_cell(case.get('test_layer'))}] "
            f"leakage={int(case.get('leakage_count') or 0)} — {_cell(case.get('query'))}"
            for case in leaked_cases[:10]
        )
    else:
        lines.append("No isolation leakage detected.")
    return "\n".join(lines)


def _extract_failures(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从逐样本记录中提取失败 case（调用错误，或可评分但 hit=0）。"""
    failures = []
    for case in cases:
        if case.get("error") is not None:
            failures.append(case)
        elif case.get("hit") == 0.0:
            failures.append(case)
    return failures


def _expected_text(case: dict[str, Any]) -> str:
    """把 case 的 expected 摘要为可读文本。"""
    expected = case.get("expected")
    if isinstance(expected, dict):
        parts = []
        gold_documents = list(expected.get("gold_document_ids") or [])
        gold_chunks = list(expected.get("gold_chunk_ids") or [])
        if gold_documents:
            parts.append(f"documents=[{_compact_ids(gold_documents)}]")
        if gold_chunks:
            parts.append(f"chunks=[{_compact_ids(gold_chunks)}]")
        if parts:
            return "; ".join(parts)
        return "no gold expectations"
    return _cell(expected)


def _retrieved_text(case: dict[str, Any]) -> str:
    """把 case 的检索结果摘要为可读文本。"""
    document_ids = list(case.get("retrieved_document_ids") or [])
    chunk_ids = list(case.get("retrieved_chunk_ids") or [])
    if not document_ids and not chunk_ids:
        return _EMPTY
    parts = []
    if document_ids:
        parts.append(f"documents=[{_compact_ids(document_ids)}]")
    if chunk_ids:
        parts.append(f"chunks=[{_compact_ids(chunk_ids)}]")
    return "; ".join(parts)


def _render_failure_cases(report: dict[str, Any]) -> str:
    failures = report.get("failure_cases")
    if not isinstance(failures, list):
        failures = _extract_failures(report.get("cases") or [])
    failures = failures[:FAILURE_LIMIT]

    lines = [_heading(2, "5. Failure Cases")]
    if not failures:
        lines.append("No failure cases in this run.")
        return "\n".join(lines)

    for index, case in enumerate(failures, 1):
        lines.append(f"{index}. **`{_cell(case.get('case_id'))}`**")
        lines.append(f"   - Dataset: {_cell(case.get('dataset'))}")
        lines.append(f"   - Category: {_cell(case.get('category'))}")
        lines.append(f"   - Test layer: {_cell(case.get('test_layer'))}")
        lines.append(f"   - Query: {_cell(case.get('query'))}")
        lines.append(f"   - Expected: {_expected_text(case)}")
        lines.append(f"   - Retrieved: {_retrieved_text(case)}")
        if case.get("error") is not None:
            lines.append(f"   - Error: `{_cell(case.get('error'))}`")
    lines.append(f"Only the first {len(failures)} failure(s) are shown; see raw JSON for all cases.")
    return "\n".join(lines)


def _render_raw_reference(source_path: str | None) -> str:
    lines = [_heading(2, "6. Raw Result Reference")]
    if source_path:
        lines.append(
            f"Detailed per-case raw results are in the JSON file produced by "
            f"`python -m evaluation.run_baseline`: `{source_path}`"
        )
    else:
        lines.append(
            "Detailed per-case raw results live in the JSON file produced by "
            "`python -m evaluation.run_baseline` (its `--output` argument)."
        )
    lines.append(
        "Inspect `cases` (including `query`/`expected`) and "
        "`breakdowns` for deeper analysis."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 顶层 API
# --------------------------------------------------------------------------
def load_report(path: Path) -> dict[str, Any]:
    """读取 run_baseline 输出的 JSON 报告。"""
    return json.loads(path.read_text(encoding="utf-8"))


def render_markdown(report: dict[str, Any], *, source_path: str | None = None) -> str:
    """把解析后的报告字典渲染为完整 Markdown 字符串。"""
    snapshot = report.get("environment_snapshot") or {}
    summary = report.get("summary") or {}
    breakdowns = report.get("breakdowns") or {}
    cases = report.get("cases") or []

    sections = [
        _heading(1, REPORT_TITLE),
        _render_snapshot(snapshot),
        _render_overall_metrics(summary),
        _render_category_metrics(breakdowns),
        _render_isolation_leakage(cases),
        _render_failure_cases(report),
        _render_raw_reference(source_path),
    ]
    return "\n\n".join(sections) + "\n"


def render_report_file(input_path: Path, output_path: Path) -> Path:
    """读取原始 JSON 报告并把渲染结果写入 output_path，返回输出路径。"""
    report = load_report(input_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_markdown(report, source_path=str(input_path)),
        encoding="utf-8",
    )
    return output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a Markdown report from run_baseline JSON output"
    )
    parser.add_argument("--input", type=Path, required=True, help="run_baseline JSON output")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="target .md path (default: input path with a .md suffix)",
    )
    args = parser.parse_args(argv)

    if not args.input.is_file():
        raise SystemExit(f"report file not found: {args.input}")
    output = args.output or args.input.with_suffix(".md")
    render_report_file(args.input, output)
    print(f"report written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
