"""Apply a reviewed placeholder mapping into a separate aligned dataset directory."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .baseline import PLACEHOLDER_RE, load_jsonl, validate_rows

STABLE_CHUNK_RE = re.compile(r"^([AB][0-9]{2})_([1-9][0-9]*)$")


def replace_placeholders(value, mapping: dict[str, object]):
    if isinstance(value, dict):
        return {
            str(replace_placeholders(key, mapping)): replace_placeholders(item, mapping)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [replace_placeholders(item, mapping) for item in value]
    if isinstance(value, str):
        if value in mapping:
            return mapping[value]
        return PLACEHOLDER_RE.sub(lambda match: str(mapping.get(match.group(0), match.group(0))), value)
    return value


def prepare_aligned_row(
    row: dict,
    mapping: dict[str, object],
    *,
    document_only: bool = False,
    chunk_annotations: dict[str, dict] | None = None,
) -> dict:
    """Replace runtime IDs and optionally drop unreliable chunk-level Gold."""
    aligned = replace_placeholders(row, mapping)
    if document_only:
        truth = aligned.get("retrieval_ground_truth")
        if isinstance(truth, dict):
            truth["gold_chunk_ids"] = []
            truth.pop("relevance_grading", None)
    elif chunk_annotations is not None:
        case_id = str(row.get("id"))
        annotation = chunk_annotations.get(case_id)
        raw_truth = row.get("retrieval_ground_truth") or {}
        if annotation is None and not raw_truth.get("gold_chunk_ids"):
            return aligned
        if not isinstance(annotation, dict) or not annotation.get("reviewed"):
            raise ValueError(f"missing reviewed chunk annotation for {case_id}")
        resolved: list[str] = []
        for stable_id in annotation.get("gold_chunks", []):
            match = STABLE_CHUNK_RE.fullmatch(str(stable_id))
            if not match:
                raise ValueError(
                    f"{case_id}: invalid stable chunk annotation {stable_id!r}"
                )
            doc_tag, number_text = match.groups()
            document_id = mapping.get(f"<DOC_{doc_tag}>")
            if not isinstance(document_id, int):
                raise ValueError(
                    f"{case_id}: missing integer mapping for <DOC_{doc_tag}>"
                )
            resolved.append(f"{document_id}_{int(number_text) - 1}")
        if not resolved:
            raise ValueError(f"{case_id}: reviewed gold_chunks must not be empty")
        truth = aligned.get("retrieval_ground_truth")
        if not isinstance(truth, dict):
            raise ValueError(f"{case_id}: retrieval_ground_truth must be an object")
        truth["gold_chunk_ids"] = resolved
        truth["relevance_grading"] = {
            chunk_id: 2 for chunk_id in resolved
        }
    return aligned


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--document-only",
        action="store_true",
        help=(
            "clear gold_chunk_ids/relevance_grading after ID replacement; "
            "use when runtime chunk boundaries have not been manually aligned"
        ),
    )
    parser.add_argument(
        "--chunk-annotations",
        type=Path,
        help=(
            "reviewed stable chunk annotations; resolves A01_1-style IDs "
            "against the runtime document mapping"
        ),
    )
    parser.add_argument("dataset", type=Path, nargs="+")
    args = parser.parse_args()
    if args.document_only and args.chunk_annotations:
        parser.error("--document-only and --chunk-annotations are mutually exclusive")
    mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
    chunk_annotations = None
    if args.chunk_annotations:
        annotation_payload = json.loads(
            args.chunk_annotations.read_text(encoding="utf-8")
        )
        chunk_annotations = annotation_payload.get("cases")
        if not isinstance(chunk_annotations, dict):
            parser.error("chunk annotation file must contain an object field 'cases'")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for path in args.dataset:
        aligned = [
            prepare_aligned_row(
                row,
                mapping,
                document_only=args.document_only,
                chunk_annotations=chunk_annotations,
            )
            for row in load_jsonl(path)
        ]
        errors = validate_rows(aligned, source=path.name, allow_placeholders=False)
        if errors:
            raise SystemExit("alignment validation failed:\n" + "\n".join(errors[:30]))
        target = args.output_dir / path.name
        target.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in aligned),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
