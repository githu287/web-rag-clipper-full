from __future__ import annotations

import unittest

from evaluation.baseline import (
    CaseResult,
    _metric_values,
    build_report,
    build_score_threshold_analysis,
    summarize,
    validate_rows,
)
from evaluation.align_datasets import prepare_aligned_row
from evaluation.validate_runtime_alignment import (
    RuntimeDocument,
    validate_runtime_rows,
)


class EvaluationBaselineTest(unittest.TestCase):
    def test_retrieval_metrics(self) -> None:
        row = {
            "retrieval_ground_truth": {
                "gold_document_ids": [10, 20],
                "gold_chunk_ids": [],
            }
        }
        results = [
            {"document_id": 99, "id": "99_0"},
            {"document_id": 10, "id": "10_0"},
            {"document_id": 20, "id": "20_0"},
        ]
        hit, recall, precision, rr, ndcg = _metric_values(row, results, 3)
        self.assertEqual(hit, 1.0)
        self.assertEqual(recall, 1.0)
        self.assertAlmostEqual(precision, 2 / 3)
        self.assertEqual(rr, 0.5)
        self.assertGreater(ndcg, 0.0)

    def test_validator_rejects_unresolved_placeholders(self) -> None:
        rows = [{
            "id": "x",
            "question": "q",
            "mode": "current",
            "document_id": "<DOC_A01>",
            "plugin_id": "<PLUGIN_A_PID>",
            "retrieval_ground_truth": {},
        }]
        self.assertTrue(validate_rows(rows, source="test", allow_placeholders=False))
        self.assertFalse(validate_rows(rows, source="test", allow_placeholders=True))

    def test_current_mode_requires_document_id(self) -> None:
        rows = [{
            "id": "x",
            "question": "q",
            "mode": "current",
            "plugin_id": "p",
            "retrieval_ground_truth": {},
        }]
        errors = validate_rows(rows, source="test")
        self.assertTrue(any("requires document_id" in error for error in errors))

    def test_validator_rejects_malformed_placeholder(self) -> None:
        rows = [{
            "id": "x",
            "question": "q",
            "mode": "all",
            "plugin_id": "<PLUGIN_A_PID>",
            "retrieval_ground_truth": {
                "gold_document_ids": ["<DOC_A01"],
                "gold_chunk_ids": [],
            },
        }]
        errors = validate_rows(
            rows, source="test", allow_placeholders=True
        )
        self.assertTrue(any("malformed placeholder" in error for error in errors))

    def test_document_only_alignment_drops_chunk_gold(self) -> None:
        row = {
            "id": "x",
            "question": "q",
            "mode": "all",
            "plugin_id": "<PLUGIN_A_PID>",
            "retrieval_ground_truth": {
                "gold_document_ids": ["<DOC_A01>"],
                "gold_chunk_ids": ["<CHUNK_A01_5>"],
                "relevance_grading": {"<CHUNK_A01_5>": 2},
            },
        }
        aligned = prepare_aligned_row(
            row,
            {"<PLUGIN_A_PID>": "plugin-a", "<DOC_A01>": 10},
            document_only=True,
        )
        self.assertEqual(aligned["plugin_id"], "plugin-a")
        self.assertEqual(
            aligned["retrieval_ground_truth"]["gold_document_ids"], [10]
        )
        self.assertEqual(
            aligned["retrieval_ground_truth"]["gold_chunk_ids"], []
        )
        self.assertNotIn(
            "relevance_grading", aligned["retrieval_ground_truth"]
        )

    def test_document_metrics_deduplicate_repeated_chunks(self) -> None:
        row = {
            "retrieval_ground_truth": {
                "gold_document_ids": [10],
                "gold_chunk_ids": [],
            }
        }
        results = [
            {"document_id": 10, "id": "10_0"},
            {"document_id": 10, "id": "10_1"},
            {"document_id": 10, "id": "10_2"},
        ]
        hit, recall, precision, rr, ndcg = _metric_values(row, results, 3)
        self.assertEqual(hit, 1.0)
        self.assertEqual(recall, 1.0)
        self.assertAlmostEqual(precision, 1 / 3)
        self.assertEqual(rr, 1.0)
        self.assertEqual(ndcg, 1.0)

    def test_reviewed_chunk_annotations_resolve_runtime_ids(self) -> None:
        row = {
            "id": "sg001",
            "question": "q",
            "mode": "current",
            "document_id": "<DOC_A01>",
            "plugin_id": "<PLUGIN_A_PID>",
            "is_answerable": True,
            "retrieval_ground_truth": {
                "gold_document_ids": ["<DOC_A01>"],
                "gold_chunk_ids": ["<CHUNK_A01_99>"],
            },
        }
        aligned = prepare_aligned_row(
            row,
            {"<PLUGIN_A_PID>": "plugin-a", "<DOC_A01>": 10},
            chunk_annotations={
                "sg001": {"gold_chunks": ["A01_1", "A01_3"], "reviewed": True}
            },
        )
        truth = aligned["retrieval_ground_truth"]
        self.assertEqual(truth["gold_chunk_ids"], ["10_0", "10_2"])
        self.assertEqual(
            truth["relevance_grading"], {"10_0": 2, "10_2": 2}
        )

    def test_runtime_validator_checks_ownership_and_chunk_existence(self) -> None:
        row = {
            "id": "sg001",
            "question": "q",
            "mode": "current",
            "plugin_id": "plugin-a",
            "document_id": 10,
            "retrieval_ground_truth": {
                "gold_document_ids": [10],
                "gold_chunk_ids": ["10_0"],
                "forbidden_document_ids": [20],
            },
        }
        errors = validate_runtime_rows(
            [row],
            documents={
                10: RuntimeDocument(10, "plugin-a", "SUCCESS", 1),
                20: RuntimeDocument(20, "plugin-b", "SUCCESS", 1),
            },
            chunks_by_document={10: {"10_0"}, 20: {"20_0"}},
            plugin_statuses={"plugin-a": "ACTIVE", "plugin-b": "ACTIVE"},
            source="test",
        )
        self.assertEqual(errors, [])

        errors = validate_runtime_rows(
            [row],
            documents={
                10: RuntimeDocument(10, "plugin-b", "SUCCESS", 1),
                20: RuntimeDocument(20, "plugin-a", "SUCCESS", 1),
            },
            chunks_by_document={10: set(), 20: {"20_0"}},
            plugin_statuses={"plugin-a": "ACTIVE"},
            source="test",
        )
        self.assertTrue(any("belongs to another plugin" in item for item in errors))
        self.assertTrue(any("forbidden document" in item for item in errors))
        self.assertTrue(any("does not exist in Milvus" in item for item in errors))

    def test_threshold_analysis_excludes_contradicted_cases(self) -> None:
        def result(
            case_id: str,
            score: float,
            answerable: bool,
            abstention_class: str = "",
        ) -> CaseResult:
            return CaseResult(
                case_id, "d", "c", 200, 1.0, [], [], [score],
                None, None, None, None, None, 0,
                is_answerable=answerable,
                abstention_class=abstention_class,
                max_score=score,
            )

        analysis = build_score_threshold_analysis([
            result("positive", 0.8, True),
            result("unsupported", 0.2, False, "UNSUPPORTED"),
            result("contradicted", 0.9, False, "CONTRADICTED"),
        ])
        self.assertEqual(analysis["answerable"]["count"], 1)
        self.assertEqual(analysis["unsupported_or_deflected"]["count"], 1)
        self.assertEqual(
            analysis["contradicted_excluded_from_gate"]["count"], 1
        )
        self.assertEqual(
            analysis["best_observed_candidate"]["balanced_accuracy"], 1.0
        )

    def test_report_contains_breakdowns(self) -> None:
        item = CaseResult(
            "x", "rag.jsonl", "simple", 200, 1.0, [1], ["1_0"], [0.8],
            1.0, 1.0, 0.2, 1.0, 1.0, 0,
            mode="current", max_score=0.8,
        )
        report = build_report([item], 5)
        self.assertIn("rag.jsonl", report["breakdowns"]["by_dataset"])
        self.assertIn("simple", report["breakdowns"]["by_category"])
        self.assertIn("current", report["breakdowns"]["by_mode"])


if __name__ == "__main__":
    unittest.main()
