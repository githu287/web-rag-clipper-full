"""CLI: python -m evaluation.run_baseline ...

在完整数据集上执行 Retrieval 基线。相对 evaluation/baseline.py 的增量能力：

- 预热（--warmup-runs）：正式评测前先发出几次“空检索”，触发服务端
  embedding 模型 / 连接池等初始化。预热不属于被测行为，任何请求错误只
  记录并继续，不中断基线。
- 重试（--max-retries）：单条检索在“请求层异常 / 5xx”时按指数退避自动
  重试；数据 / 鉴权类失败（4xx、预期外 2xx）不重试。
- 输出增强：在原有 JSON 报告之上新增顶层 environment_snapshot，并把每个
  case 对应的 query / expected 注入 cases，供 Markdown 报告渲染使用。

核心检索调用与指标计算仍在 evaluation/baseline.py，本文件只做编排增强。
"""

from __future__ import annotations

import argparse
import datetime
import json
import platform
import time
from pathlib import Path
from typing import Any

import httpx

from .baseline import (
    CaseResult,
    build_report,
    evaluate_case,
    load_jsonl,
    validate_rows,
)

DEFAULT_WARMUP_RUNS = 3
DEFAULT_MAX_RETRIES = 3
RETRY_BASE_DELAY_SECONDS = 0.5
RETRYABLE_STATUS_CODES = frozenset({500, 502, 503, 504})


def _load_and_validate(path: Path) -> list[dict[str, Any]]:
    """加载单个数据集并做 placeholders 之外的严格校验，失败即终止。"""
    rows = load_jsonl(path)
    errors = validate_rows(rows, source=str(path), allow_placeholders=False)
    if errors:
        raise SystemExit("dataset validation failed:\n" + "\n".join(errors[:30]))
    return rows


def _plugin_secret(credentials: dict[str, Any], row: dict[str, Any]) -> str | None:
    """按行提取插件 ID 并查找其凭证；缺凭证时返回 None。"""
    plugin_id = str(row.get("target_plugin_id") or row.get("plugin_id") or "")
    secret_entry = credentials.get(plugin_id)
    secret = (
        secret_entry.get("plugin_secret")
        if isinstance(secret_entry, dict)
        else secret_entry
    )
    return str(secret) if secret else None


def _find_warmup_target(
    datasets: list[tuple[Path, list[dict[str, Any]]]],
    credentials: dict[str, Any],
) -> tuple[str, str] | None:
    """从数据集中挑选一个可用的 (plugin_id, secret) 用于预热请求。"""
    for _path, rows in datasets:
        for row in rows:
            secret = _plugin_secret(credentials, row)
            if secret:
                plugin_id = str(row.get("target_plugin_id") or row.get("plugin_id"))
                return plugin_id, secret
    return None


def run_warmup(
    client: httpx.Client,
    *,
    plugin_id: str,
    plugin_secret: str,
    top_k: int,
    warmup_runs: int,
) -> int:
    """执行若干次“空检索”预热，返回成功发出的请求数。

    预热用于让服务端先完成 embedding 模型加载、连接池建立等一次性初始化，
    避免把冷启动延迟计入首批正式用例。失败不中断基线，仅打印提示。
    """
    completed = 0
    headers = {"X-Plugin-ID": plugin_id, "X-Plugin-Secret": plugin_secret}
    for index in range(1, warmup_runs + 1):
        try:
            client.post(
                "/rag/search",
                json={"query": "", "limit": top_k},
                headers=headers,
            )
            completed += 1
        except httpx.HTTPError as exc:
            print(f"[warmup] request {index}/{warmup_runs} failed (ignored): {exc}")
    return completed


def _is_retryable(result: CaseResult) -> bool:
    """判断一次失败是否值得重试。

    status_code == 0 表示请求层异常（连接失败、超时等）；
    5xx 表示服务端瞬时错误。4xx 与数据问题不重试。
    """
    return result.status_code == 0 or result.status_code in RETRYABLE_STATUS_CODES


def evaluate_case_with_retry(
    client: httpx.Client,
    row: dict[str, Any],
    *,
    dataset_name: str,
    plugin_secret: str,
    top_k: int,
    max_retries: int,
) -> CaseResult:
    """带指数退避重试的 evaluate_case 包装（仅重试可恢复的失败）。"""
    result = evaluate_case(
        client,
        row,
        dataset_name=dataset_name,
        plugin_secret=plugin_secret,
        top_k=top_k,
    )
    case_id = str(row.get("id"))
    for attempt in range(1, max_retries + 1):
        if not _is_retryable(result):
            return result
        delay = RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
        message = (result.error or "")[:120]
        print(
            f"[retry] case={case_id} attempt={attempt}/{max_retries} "
            f"status={result.status_code}; backing off {delay:.1f}s: {message}"
        )
        time.sleep(delay)
        result = evaluate_case(
            client,
            row,
            dataset_name=dataset_name,
            plugin_secret=plugin_secret,
            top_k=top_k,
        )
    return result


def build_environment_snapshot(
    *,
    dataset_names: list[str],
    top_k: int,
    warmup_runs: int,
) -> dict[str, Any]:
    """构造写入 JSON 顶层 environment_snapshot 字段的环境信息。"""
    return {
        "timestamp": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "dataset_name": ", ".join(dataset_names),
        "top_k": top_k,
        "warmup_runs": warmup_runs,
    }


def annotate_cases(report: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    """把每个 case 对应的 query / expected 注入 cases。

    baseline.build_report 的逐样本记录不含问题原文与金标（避免原始 gold
    进入公共 schema）；此处按行序把二者并回 cases，供 Markdown 报告的
    Failure Cases 章节渲染，属纯增量字段。
    """
    for case, row in zip(report["cases"], rows):
        truth = row.get("retrieval_ground_truth") or {}
        case["query"] = str(row.get("question") or "")
        case["expected"] = {
            "gold_document_ids": list(truth.get("gold_document_ids") or []),
            "gold_chunk_ids": list(truth.get("gold_chunk_ids") or []),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run retrieval evaluation baseline")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--warmup-runs",
        type=int,
        default=DEFAULT_WARMUP_RUNS,
        help="empty searches before the run (default: %(default)s)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help="retries per failed case with exponential backoff (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    credentials = json.loads(args.credentials.read_text(encoding="utf-8"))
    datasets = [(_path, _load_and_validate(_path)) for _path in args.dataset]

    with httpx.Client(base_url=args.base_url, timeout=args.timeout) as client:
        warmup_done = 0
        if args.warmup_runs > 0:
            warmup_target = _find_warmup_target(datasets, credentials)
            if warmup_target is None:
                print("[warmup] skipped: no credential found in dataset rows")
            else:
                plugin_id, plugin_secret = warmup_target
                warmup_done = run_warmup(
                    client,
                    plugin_id=plugin_id,
                    plugin_secret=plugin_secret,
                    top_k=args.top_k,
                    warmup_runs=args.warmup_runs,
                )
                print(f"[warmup] {warmup_done}/{args.warmup_runs} requests completed")

        all_results: list[CaseResult] = []
        source_rows: list[dict[str, Any]] = []
        for path, rows in datasets:
            for row in rows:
                secret = _plugin_secret(credentials, row)
                if not secret:
                    plugin_id = row.get("target_plugin_id") or row.get("plugin_id")
                    raise SystemExit(f"missing credential for plugin_id={plugin_id}")
                source_rows.append(row)
                all_results.append(
                    evaluate_case_with_retry(
                        client,
                        row,
                        dataset_name=path.name,
                        plugin_secret=secret,
                        top_k=args.top_k,
                        max_retries=args.max_retries,
                    )
                )

    report = build_report(all_results, args.top_k)
    annotate_cases(report, source_rows)
    report["environment_snapshot"] = build_environment_snapshot(
        dataset_names=[path.name for path, _ in datasets],
        top_k=args.top_k,
        warmup_runs=warmup_done,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
