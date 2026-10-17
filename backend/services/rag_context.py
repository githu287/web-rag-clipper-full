"""Pack retrieved evidence and its sources under one character budget."""
from collections.abc import Sequence

from ..models.api_schema import RagSearchResult


_SEPARATOR = "\n\n---\n\n"


def build_context(
    results: Sequence[RagSearchResult], max_chars: int,
) -> tuple[str, list[RagSearchResult]]:
    """Return context and exactly the ordered sources with body text in it.

    Headers are never cut in half. Blank or repeated chunks are skipped, and
    oversized metadata cannot consume the entire normal context budget.
    The last included body may be shortened to fit the remaining space.
    """
    blocks: list[str] = []
    included: list[RagSearchResult] = []
    seen: set[str] = set()
    remaining = max_chars
    for result in results:
        if result.id in seen or not result.chunk_text.strip():
            continue
        title = " ".join((result.title or "").split())[:200]
        url = " ".join((result.url or "").split())[:500]
        header = (
            f"[Source {len(included) + 1}]\n"
            f"title: {title}\nurl: {url}\n"
            f"document_id: {result.document_id}\nchunk:\n"
        )
        separator_size = len(_SEPARATOR) if blocks else 0
        body_budget = remaining - separator_size - len(header)
        if body_budget <= 0:
            continue
        body = result.chunk_text[:body_budget].rstrip()
        if not body.strip():
            continue
        block = header + body
        blocks.append(block)
        included.append(result)
        seen.add(result.id)
        remaining -= separator_size + len(block)
    return _SEPARATOR.join(blocks), included
