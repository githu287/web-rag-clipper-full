"""Prompt/source consistency and budget regressions; no external API calls."""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.models.api_schema import RagSearchResult
from backend.services.rag_answer import RagAnswerService
from backend.services.rag_context import build_context


def chunk(index=0, text="evidence", title="Title", url="https://example.com"):
    return RagSearchResult(
        id=f"1_{index}", page_id=1, chunk_index=index, chunk_text=text,
        distance=0.9, document_id=1, filename="note.md", status="SUCCESS", content_revision=0,
        created_at=datetime(2026, 1, 1), title=title, url=url, source_type="webpage",
    )


def answer(results, budget=4000):
    llm = Mock(generate=Mock(return_value="answer [Source 1]"))
    documents = Mock()
    documents.get_documents_by_ids.return_value = [SimpleNamespace(id=1, status="SUCCESS", content_revision=0)]
    service = RagAnswerService(
        SimpleNamespace(search=AsyncMock(return_value=results)), llm, documents,
        max_context_chars=budget,
    )
    response = asyncio.run(service.ask("question", plugin_id="test", api_key="test"))
    return response, llm


def test_sources_exclude_chunks_cut_off_by_budget():
    first, second = chunk(text="first"), chunk(1, "second")
    first_context, _ = build_context([first], 4000)
    # Remaining space cannot fit a second source header and body.
    response, llm = answer([first, second], len(first_context) + 10)
    assert [source.chunk_id for source in response.sources] == [first.id]
    prompt = llm.generate.call_args.args[1]
    assert "[Source 1]" in prompt
    assert "[Source 2]" not in prompt
    assert "second" not in prompt


def test_partial_last_body_keeps_complete_header_and_corresponding_source():
    first, second = chunk(text="first"), chunk(1, "second body " * 100)
    full, _ = build_context([first, second], 4000)
    budget = full.index("second body") + 6
    context, included = build_context([first, second], budget)
    assert len(context) == budget
    assert context.endswith("second")
    assert [result.id for result in included] == [first.id, second.id]
    response, _ = answer([first, second], budget)
    assert [source.chunk_id for source in response.sources] == [first.id, second.id]


@pytest.mark.parametrize("budget", [0, 1, 10, 30])
def test_no_body_fits_skips_llm_and_returns_no_sources(budget):
    response, llm = answer([chunk()], budget)
    assert response.sources == []
    llm.generate.assert_not_called()


def test_blank_and_duplicate_chunks_do_not_consume_source_numbers():
    first, second = chunk(text="first"), chunk(1, "second")
    response, llm = answer([chunk(2, " \n "), first, first, second])
    assert [source.chunk_id for source in response.sources] == [first.id, second.id]
    prompt = llm.generate.call_args.args[1]
    assert prompt.count("[Source 1]") == 1
    assert prompt.count("[Source 2]") == 1
    assert "[Source 3]" not in prompt


def test_long_metadata_cannot_crowd_out_body_and_sources_keep_original_metadata():
    result = chunk(title="Title\n" * 80, url="https://example.com/" + "x" * 1000)
    response, llm = answer([result])
    prompt = llm.generate.call_args.args[1]
    assert "chunk:\nevidence" in prompt
    assert response.sources[0].title == result.title
    assert response.sources[0].url == result.url


def test_smaller_later_source_can_fit_when_earlier_header_does_not():
    large = chunk(title="t" * 200, url="https://example.com/" + "x" * 500)
    small = chunk(1, "small", title="", url="")
    context, included = build_context([large, small], 100)
    assert [item.id for item in included] == [small.id]
    assert "[Source 1]" in context
    assert "[Source 2]" not in context


def test_question_is_not_written_to_normal_application_logs(caplog):
    with caplog.at_level("INFO", logger="backend.services.rag_answer"):
        answer([chunk()])
    assert "question" not in caplog.text
