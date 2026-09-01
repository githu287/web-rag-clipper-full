"""Retrieval Evaluation Baseline：数据校验、API 执行、指标聚合。"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import httpx

PLACEHOLDER_RE = re.compile(r"<[A-Z0-9_]+>")
PLACEHOLDER_MARKERS = ("<PLUGIN_", "<DOC_", "<CHUNK_")


class DatasetValidationError(ValueError):
    pass


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    dataset: str
    category: str
    status_code: int
    latency_ms: float
    retrieved_document_ids: list[int | str]
    retrieved_chunk_ids: list[str]
    scores: list[float]
    hit: float | None
    recall: float | None
    precision: float | None
    reciprocal_rank: float | None
    ndcg: float | None
    leakage_count: int
    error: str | None = None
    mode: str = ""
    test_layer: str = ""
    is_answerable: bool | None = None
    abstention_class: str = ""
    max_score: float | None = None


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DatasetValidationError(f"{path}:{line_number}: invalid JSON") from exc
        if not isinstance(row, dict):
            raise DatasetValidationError(f"{path}:{line_number}: row must be an object")
        rows.append(row)
    return rows


def validate_rows(
    rows: Iterable[dict[str, Any]],
    *,
    source: str,
    allow_placeholders: bool = False,
) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, 1):
        prefix = f"{source}:{index}"
        required = ["id", "question", "mode"]
        if row.get("test_layer") != "L5_CrossWorkspace_Ownership":
            required.append("retrieval_ground_truth")
        for field in required:
            if field not in row:
                errors.append(f"{prefix}: missing {field}")
        case_id = row.get("id")
        if case_id in seen:
            errors.append(f"{prefix}: duplicate id {case_id}")
        if isinstance(case_id, str):
            seen.add(case_id)
        if row.get("mode") not in {"current", "all"}:
            errors.append(f"{prefix}: mode must be current/all")
        if row.get("mode") == "current" and "document_id" not in row:
            errors.append(f"{prefix}: current mode requires document_id")
        serialized = json.dumps(row)
        without_valid_placeholders = PLACEHOLDER_RE.sub("", serialized)
        if any(
            marker in without_valid_placeholders
            for marker in PLACEHOLDER_MARKERS
        ):
            errors.append(f"{prefix}: malformed placeholder")
        elif not allow_placeholders and PLACEHOLDER_RE.search(serialized):
            errors.append(f"{prefix}: unresolved placeholder")
    return errors


def _metric_values(
    row: dict[str, Any], results: list[dict[str, Any]], top_k: int
) -> tuple[float | None, float | None, float | None, float | None, float | None]:
    truth = row.get("retrieval_ground_truth") or {}
    gold_docs = {str(value) for value in truth.get("gold_document_ids", [])}
    gold_chunks = {str(value) for value in truth.get("gold_chunk_ids", [])}
    relevant = gold_chunks or gold_docs
    if not relevant:
        return None, None, None, None, None

    ranked = [
        str(item.get("id")) if gold_chunks else str(item.get("document_id", item.get("page_id")))
        for item in results[:top_k]
    ]
    if not gold_chunks:
        # The API ranks chunks, so one document may occupy several positions.
        # Document-level evaluation must score unique documents; otherwise one
        # relevant document can add gain repeatedly and yield nDCG > 1.
        ranked = list(dict.fromkeys(ranked))
    matches = [item in relevant for item in ranked]
    hit = float(any(matches))
    recall = sum(1 for item in relevant if item in ranked) / len(relevant)
    precision = sum(matches) / top_k if top_k > 0 else 0.0
    reciprocal_rank = next((1.0 / rank for rank, ok in enumerate(matches, 1) if ok), 0.0)

    grading = {str(k): float(v) for k, v in (truth.get("relevance_grading") or {}).items()}
    gains = [grading.get(item, 1.0 if item in relevant else 0.0) for item in ranked]
    dcg = sum((2**gain - 1) / math.log2(rank + 1) for rank, gain in enumerate(gains, 1))
    ideal_gains = sorted(
        grading.values() if grading else [1.0] * len(relevant), reverse=True
    )[:top_k]
    idcg = sum(
        (2**gain - 1) / math.log2(rank + 1)
        for rank, gain in enumerate(ideal_gains, 1)
    )
    ndcg = dcg / idcg if idcg else 0.0
    return hit, recall, precision, reciprocal_rank, ndcg


def evaluate_case(
    client: httpx.Client,
    row: dict[str, Any],
    *,
    dataset_name: str,
    plugin_secret: str,
    top_k: int,
) -> CaseResult:
    plugin_id = row.get("target_plugin_id") or row.get("plugin_id")
    payload: dict[str, Any] = {"query": row["question"], "limit": top_k}
    if row.get("mode") == "current":
        payload["document_id"] = row["document_id"]
    started = time.perf_counter()
    metadata = {
        "mode": str(row.get("mode", "")),
        "test_layer": str(row.get("test_layer", "")),
        "is_answerable": row.get("is_answerable"),
        "abstention_class": str(
            (row.get("negative_eval") or {}).get(
                "four_way_abstention_class_expected", ""
            )
        ),
    }
    try:
        response = client.post(
            "/rag/search",
            json=payload,
            headers={"X-Plugin-ID": str(plugin_id), "X-Plugin-Secret": plugin_secret},
        )
        latency_ms = (time.perf_counter() - started) * 1000
        expected_status = int(
            row.get("expected_status_code")
            or (row.get("expected_isolation_result") or {}).get("L5_expected_status_code", 200)
        )
        if response.status_code != expected_status:
            return CaseResult(
                str(row.get("id")), dataset_name, str(row.get("category", "")),
                response.status_code, latency_ms, [], [], [], None, None, None, None,
                None, 0, f"expected HTTP {expected_status}: {response.text[:500]}",
                **metadata,
            )
        if response.status_code != 200:
            return CaseResult(
                str(row.get("id")), dataset_name, str(row.get("category", "")),
                response.status_code, latency_ms, [], [], [], None, None, None, None,
                None, 0, **metadata,
            )
        results = response.json().get("results", [])
        hit, recall, precision, rr, ndcg = _metric_values(row, results, top_k)
        truth = row.get("retrieval_ground_truth") or {}
        forbidden_docs = {str(v) for v in truth.get("forbidden_document_ids", [])}
        forbidden_terms = [str(v).lower() for v in truth.get("forbidden_terms_in_retrieved_text", [])]
        leakage_count = 0
        for item in results:
            if str(item.get("document_id", item.get("page_id"))) in forbidden_docs:
                leakage_count += 1
            text = str(item.get("chunk_text", "")).lower()
            if any(term in text for term in forbidden_terms):
                leakage_count += 1
        return CaseResult(
            str(row.get("id")), dataset_name, str(row.get("category", "")),
            response.status_code, latency_ms,
            [item.get("document_id", item.get("page_id")) for item in results],
            [str(item.get("id")) for item in results],
            [float(item.get("distance", 0.0)) for item in results],
            hit, recall, precision, rr, ndcg, leakage_count,
            **metadata,
            max_score=(
                max(float(item.get("distance", 0.0)) for item in results)
                if results
                else None
            ),
        )
    except Exception as exc:
        return CaseResult(
            str(row.get("id")), dataset_name, str(row.get("category", "")),
            0, (time.perf_counter() - started) * 1000, [], [], [], None, None,
            None, None, None, 0, f"{type(exc).__name__}: {exc}",
            **metadata,
        )


def summarize(results: list[CaseResult], top_k: int) -> dict[str, Any]:
    def average(name: str) -> float | None:
        values = [getattr(item, name) for item in results if getattr(item, name) is not None]
        return round(sum(values) / len(values), 6) if values else None

    latencies = sorted(item.latency_ms for item in results)
    p95_index = max(0, math.ceil(len(latencies) * 0.95) - 1) if latencies else 0
    return {
        "top_k": top_k,
        "case_count": len(results),
        "successful_case_count": sum(item.error is None for item in results),
        "error_rate": round(sum(item.error is not None for item in results) / len(results), 6) if results else 0.0,
        "hit_rate": average("hit"),
        "recall_at_k": average("recall"),
        "precision_at_k": average("precision"),
        "mrr": average("reciprocal_rank"),
        "ndcg_at_k": average("ndcg"),
        "isolation_leakage_count": sum(item.leakage_count for item in results),
        "latency_ms_mean": round(sum(latencies) / len(latencies), 3) if latencies else None,
        "latency_ms_p95": round(latencies[p95_index], 3) if latencies else None,
    }


def _score_distribution(values: list[float]) -> dict[str, float | int | None]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "min": None, "mean": None, "p95": None, "max": None}
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return {
        "count": len(ordered),
        "min": round(ordered[0], 6),
        "mean": round(sum(ordered) / len(ordered), 6),
        "p95": round(ordered[p95_index], 6),
        "max": round(ordered[-1], 6),
    }


def build_score_threshold_analysis(results: list[CaseResult]) -> dict[str, Any]:
    """Analyze a retrieval score gate without treating contradicted queries as empty."""
    positives = [
        item.max_score
        for item in results
        if item.is_answerable is True and item.max_score is not None
    ]
    eligible_negatives = [
        item.max_score
        for item in results
        if item.is_answerable is False
        and item.abstention_class in {"UNSUPPORTED", "DEFLECTED"}
        and item.max_score is not None
    ]
    contradicted = [
        item.max_score
        for item in results
        if item.is_answerable is False
        and item.abstention_class == "CONTRADICTED"
        and item.max_score is not None
    ]
    candidates = sorted(set(positives + eligible_negatives))
    best: dict[str, float | int] | None = None
    for threshold in candidates:
        true_positive = sum(score >= threshold for score in positives)
        true_negative = sum(score < threshold for score in eligible_negatives)
        false_negative = len(positives) - true_positive
        false_positive = len(eligible_negatives) - true_negative
        sensitivity = true_positive / len(positives) if positives else 0.0
        specificity = (
            true_negative / len(eligible_negatives) if eligible_negatives else 0.0
        )
        candidate = {
            "threshold": round(threshold, 6),
            "balanced_accuracy": round((sensitivity + specificity) / 2, 6),
            "false_reject_count": false_negative,
            "false_accept_count": false_positive,
            "answerable_recall": round(sensitivity, 6),
            "unsupported_rejection_rate": round(specificity, 6),
        }
        if best is None or (
            candidate["balanced_accuracy"],
            -candidate["false_reject_count"],
        ) > (
            best["balanced_accuracy"],
            -best["false_reject_count"],
        ):
            best = candidate
    return {
        "answerable": _score_distribution([float(value) for value in positives]),
        "unsupported_or_deflected": _score_distribution(
            [float(value) for value in eligible_negatives]
        ),
        "contradicted_excluded_from_gate": _score_distribution(
            [float(value) for value in contradicted]
        ),
        "best_observed_candidate": best,
        "note": (
            "Exploratory only: CONTRADICTED queries need retrieval evidence to rebut "
            "the false premise and are excluded from threshold fitting."
        ),
    }


def _grouped_summaries(
    results: list[CaseResult], top_k: int, attribute: str
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[CaseResult]] = {}
    for item in results:
        key = str(getattr(item, attribute) or "")
        if key:
            groups.setdefault(key, []).append(item)
    return {
        key: summarize(items, top_k)
        for key, items in sorted(groups.items())
    }


def build_report(results: list[CaseResult], top_k: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at_epoch": int(time.time()),
        "summary": summarize(results, top_k),
        "breakdowns": {
            "by_dataset": _grouped_summaries(results, top_k, "dataset"),
            "by_category": _grouped_summaries(results, top_k, "category"),
            "by_mode": _grouped_summaries(results, top_k, "mode"),
            "by_test_layer": _grouped_summaries(results, top_k, "test_layer"),
        },
        "score_threshold_analysis": build_score_threshold_analysis(results),
        "cases": [asdict(item) for item in results],
    }
