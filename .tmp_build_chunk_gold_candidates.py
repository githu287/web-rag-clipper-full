import json
import re
from pathlib import Path

from pymilvus import MilvusClient

from backend.core.config import get_settings


root = Path("evaluation")
mapping = json.loads(
    (root / "private" / "dataset_placeholder_mapping.json").read_text(
        encoding="utf-8"
    )
)
rows = [
    json.loads(line)
    for line in (root / "datasets" / "rag_eval.jsonl")
    .read_text(encoding="utf-8")
    .splitlines()
    if line.strip()
]


def features(text: str) -> tuple[set[str], set[str]]:
    lowered = text.lower()
    ascii_tokens = set(re.findall(r"[a-z0-9_@.-]{2,}", lowered))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    grams = {
        chinese[index : index + size]
        for size in (2, 3, 4)
        for index in range(max(0, len(chinese) - size + 1))
    }
    return ascii_tokens, grams


def similarity(reference: str, chunk: str) -> float:
    ref_ascii, ref_grams = features(reference)
    chunk_ascii, chunk_grams = features(chunk)
    ascii_score = (
        len(ref_ascii & chunk_ascii) / len(ref_ascii) if ref_ascii else 0.0
    )
    gram_score = (
        len(ref_grams & chunk_grams) / len(ref_grams) if ref_grams else 0.0
    )
    if ref_ascii and ref_grams:
        return 0.45 * ascii_score + 0.55 * gram_score
    return ascii_score or gram_score


settings = get_settings()
client = MilvusClient(
    uri=f"http://{settings.milvus_host}:{settings.milvus_port}"
)
cache = {}
try:
    for key, value in mapping.items():
        match = re.fullmatch(r"<DOC_([AB][0-9]{2})>", key)
        if not match:
            continue
        doc_tag = match.group(1)
        chunks = client.query(
            collection_name=settings.milvus_collection,
            filter=f"page_id == {int(value)}",
            output_fields=["id", "page_id", "chunk_index", "chunk_text"],
        )
        chunks.sort(key=lambda item: item["chunk_index"])
        cache[doc_tag] = chunks
finally:
    client.close()

candidates = {}
low_confidence = []
for row in rows:
    if not row.get("is_answerable", True):
        continue
    doc_tags = []
    for placeholder in row["retrieval_ground_truth"]["gold_document_ids"]:
        match = re.fullmatch(r"<DOC_([AB][0-9]{2})>", str(placeholder))
        if not match:
            raise RuntimeError(f"{row['id']}: invalid gold doc {placeholder}")
        doc_tags.append(match.group(1))
    chunk_pool = [
        (doc_tag, chunk)
        for doc_tag in doc_tags
        for chunk in cache[doc_tag]
    ]
    references = list(
        (row.get("context_ground_truth") or {}).get("information_points") or []
    )
    if not references:
        references = list(
            (row.get("answer_ground_truth") or {}).get("key_points") or []
        )
    selected = {}
    evidence = []
    for reference in references:
        ranked = sorted(
            (
                (similarity(str(reference), chunk["chunk_text"]), doc_tag, chunk)
                for doc_tag, chunk in chunk_pool
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        score, doc_tag, chunk = ranked[0]
        stable_id = f"{doc_tag}_{int(chunk['chunk_index']) + 1}"
        selected[stable_id] = {
            "document": doc_tag,
            "chunk_number": int(chunk["chunk_index"]) + 1,
        }
        evidence.append(
            {
                "information_point": reference,
                "selected": stable_id,
                "score": round(score, 4),
                "runner_up": (
                    f"{ranked[1][1]}_{int(ranked[1][2]['chunk_index']) + 1}"
                    if len(ranked) > 1
                    else None
                ),
                "runner_up_score": (
                    round(ranked[1][0], 4) if len(ranked) > 1 else None
                ),
            }
        )
        if score < 0.08:
            low_confidence.append(
                {"case_id": row["id"], "point": reference, "score": score}
            )
    candidates[row["id"]] = {
        "documents": doc_tags,
        "gold_chunks": list(selected.values()),
        "evidence": evidence,
    }

output = root / "private" / "chunk_gold_candidates.json"
output.write_text(
    json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(f"answerable_cases={len(candidates)}")
print(f"low_confidence_points={len(low_confidence)}")
for item in low_confidence:
    print(
        f"LOW {item['case_id']} score={item['score']:.4f} "
        f"point={item['point']}"
    )

# The lexical pass only proposes candidates. These overrides are the result of
# reviewing the selected runtime chunk text against every information point.
reviewed_overrides = {
    "sg051": ["A05_1"],
    "sg052": ["A16_2"],
    "sg055": ["A16_4"],
    "sg056": ["A05_4"],
    "sg057": ["A16_3", "A16_4"],
    "sg058": ["A16_2"],
    "sg060": ["A16_2"],
    "sg061": ["A01_2", "A03_2", "A03_3", "A04_3"],
    "sg067": ["A12_3", "A14_1", "A14_3"],
    "sg068": [
        "A10_1", "A10_2", "A10_3", "A10_4",
        "A09_1", "A09_3", "A19_1", "A19_2", "A05_1",
        "A16_2", "A18_1", "A18_2", "A17_1", "A17_2",
        "A13_1", "A13_2", "A20_2", "A20_3",
    ],
    "sg069": ["A16_2", "A16_4", "A17_3", "A20_3"],
}

annotation_cases = {}
for case_id, candidate in candidates.items():
    stable_chunks = reviewed_overrides.get(
        case_id,
        [
            f"{item['document']}_{item['chunk_number']}"
            for item in candidate["gold_chunks"]
        ],
    )
    seen = set()
    stable_chunks = [
        item for item in stable_chunks
        if not (item in seen or seen.add(item))
    ]
    represented = {item.rsplit("_", 1)[0] for item in stable_chunks}
    missing_docs = set(candidate["documents"]) - represented
    if missing_docs:
        raise RuntimeError(
            f"{case_id}: reviewed chunks do not cover documents "
            f"{sorted(missing_docs)}"
        )
    for stable_id in stable_chunks:
        doc_tag, number_text = stable_id.rsplit("_", 1)
        number = int(number_text)
        if number < 1 or number > len(cache[doc_tag]):
            raise RuntimeError(
                f"{case_id}: invalid reviewed chunk {stable_id}"
            )
    annotation_cases[case_id] = {
        "gold_chunks": stable_chunks,
        "reviewed": True,
    }

annotation_output = root / "datasets" / "chunk_gold_annotations.json"
annotation_output.write_text(
    json.dumps(
        {
            "schema_version": 1,
            "chunk_config": {"chunk_size": 700, "chunk_overlap": 100},
            "case_count": len(annotation_cases),
            "cases": annotation_cases,
        },
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)
print(f"reviewed_annotations={len(annotation_cases)}")
