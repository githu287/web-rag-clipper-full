"""Bounded display context for consecutive ASR rows; never widen source scope."""
import math
import json


def continuous_transcript(rows, cited_ids=None):
    """Join only adjacent, temporally close rows explicitly available to this call.

    ASR often puts a sentence's subject and predicate in separate rows. Keep the
    original text, IDs and timestamps unchanged; expose their concatenation as
    additional reading context, not as a new source or a corrected transcript.
    When reviewing, every member must be explicitly cited by that answer.
    """
    passages, group = [], []
    context_chars = 0

    def flush():
        nonlocal context_chars
        if len(group) > 1:
            passage = {"sources": [f"[原文 {r['id']}]" for r in group],
                       "text": " ".join(r["text"] for r in group)}
            size = len(json.dumps(passage, ensure_ascii=False))
            if context_chars + size <= 6000:
                passages.append(passage)
                context_chars += size

    for row in rows:
        start, end = row.get("start_seconds"), row.get("end_seconds")
        valid = (type(row.get("id")) is int and isinstance(row.get("text"), str)
                 and row["text"].strip() and all(type(v) in (int, float) and math.isfinite(v) for v in (start, end))
                 and 0 <= start < end and (cited_ids is None or row["id"] in cited_ids))
        if not valid:
            flush()
            group = []
            continue
        if group:
            previous = group[-1]
            adjacent = (row["id"] == previous["id"] + 1
                        and 0 <= start - previous["end_seconds"] <= 2
                        and len(group) < 4 and end - group[0]["start_seconds"] <= 30
                        and sum(len(r["text"]) for r in group) + len(row["text"]) <= 1000)
            if not adjacent:
                flush()
                group = []
        group.append(row)
    flush()
    return passages
