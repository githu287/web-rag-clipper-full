"""Real SQL revision fencing; only vector/model boundaries are simulated."""
import asyncio
import importlib.util
from datetime import datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session

from backend.api.deps import get_current_plugin
from backend.core.di import get_plugin_service, get_rag_answer_service
from backend.core.exceptions import DocumentNotFoundError, DocumentOperationError
from backend.main import create_app
from backend.models.api_schema import RagSearchResult
from backend.models.base import Base
from backend.models.document import DocumentStatus as DS
from backend.models.document import Document
from backend.models.milvus_dto import ChunkSearchResult
from backend.repositories.mysql import DocumentRepositoryImpl, PluginRepositoryImpl, IngestJobRepositoryImpl
from backend.services.rag import RagService
from backend.services.rag_answer import RagAnswerService


@pytest.fixture
def store():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    plugins = PluginRepositoryImpl(engine)
    for owner in ("a", "b"):
        plugins.create_plugin(owner, owner, owner, owner * 64)
    documents = DocumentRepositoryImpl(engine)
    doc = documents.create_document("clip.txt", "", "a", source_type="webpage", url="https://example.com")
    doc = documents.update_ingest_result(doc.id, chunk_count=1, status=DS.SUCCESS)
    yield engine, documents, doc
    engine.dispose()


def migration():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/0015_document_content_revision.py"
    spec = importlib.util.spec_from_file_location("document_revision_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_preserves_old_rows_and_defaults_new_rows():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE documents (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        connection.exec_driver_sql("INSERT INTO documents VALUES (1, 'old evidence')")
        with Operations.context(MigrationContext.configure(connection)):
            migration().upgrade()
            connection.exec_driver_sql("INSERT INTO documents (id, body) VALUES (2, 'new evidence')")
            assert connection.exec_driver_sql("SELECT body, content_revision FROM documents ORDER BY id").all() == [
                ("old evidence", 0), ("new evidence", 0),
            ]
            migration().downgrade()
            assert "content_revision" not in {c["name"] for c in inspect(connection).get_columns("documents")}
            assert connection.exec_driver_sql("SELECT body FROM documents ORDER BY id").scalars().all() == ["old evidence", "new evidence"]
    engine.dispose()


def test_mysql_migration_uses_non_null_bigint_with_zero_backfill():
    output = StringIO()
    context = MigrationContext.configure(dialect_name="mysql", opts={"as_sql": True, "output_buffer": output})
    with Operations.context(context):
        migration().upgrade()
    sql = output.getvalue()
    assert "ADD COLUMN content_revision BIGINT" in sql
    assert "DEFAULT '0'" in sql and "NOT NULL" in sql


def test_revisions_advance_on_orm_and_core_writers_including_worker_claim(store):
    engine, documents, doc = store
    revision = doc.content_revision

    def advance():
        nonlocal revision
        current = documents.get_document(doc.id, "a").content_revision
        assert current > revision
        revision = current

    documents.transition_status(doc.id, "a", {DS.SUCCESS}, DS.PROCESSING)
    advance()
    documents.update_ingest_result(doc.id, chunk_count=1, status=DS.SUCCESS)
    advance()
    documents.update_webpage_metadata(doc.id, "a", title="Updated title", url="https://example.com")
    advance()
    documents.update_status(doc.id, DS.FAILED)
    advance()
    jobs = IngestJobRepositoryImpl(engine)
    job = jobs.create_job("a", "WEB_CLIP", doc.id)
    jobs.claim_job(job.id)
    jobs.begin_document(job.id, doc.id)
    advance()
    jobs.mark_failed(job.id, "fixture failure")
    advance()
    documents.update_failure(doc.id, error_message="new fixture failure")
    advance()
    with pytest.raises(DocumentNotFoundError):
        documents.transition_status(doc.id, "b", {DS.FAILED}, DS.PROCESSING)
    assert documents.get_document(doc.id, "a").content_revision == revision


def test_revision_snapshot_filters_owner_and_status_in_one_query(store):
    engine, documents, doc = store
    foreign = documents.create_document("other.txt", "", "b")
    documents.update_ingest_result(foreign.id, chunk_count=1, status=DS.SUCCESS)
    pending = documents.create_document("pending.txt", "", "a")
    statements = []
    def record(*args):
        statements.append(args[2])
    event.listen(engine, "before_cursor_execute", record)
    try:
        assert documents.get_content_revisions([doc.id, foreign.id, pending.id, 999], "a") == {doc.id: doc.content_revision}
        assert len(statements) == 1
        assert documents.get_content_revisions([], "a") == {}
        assert len(statements) == 1
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_stale_orm_instances_do_not_overwrite_each_others_revision_increment(store):
    engine, documents, doc = store
    with Session(engine) as first, Session(engine) as second:
        left, right = first.get(Document, doc.id), second.get(Document, doc.id)
        assert left.content_revision == right.content_revision == doc.content_revision
        left.title = "First metadata update"
        first.commit()
        right.title = "Second metadata update"
        second.commit()
    assert documents.get_document(doc.id, "a").content_revision == doc.content_revision + 2


def services(documents, doc, callback=None):
    chunk = ChunkSearchResult(id=f"{doc.id}_0", page_id=doc.id, chunk_index=0,
                              chunk_text="验收编号为806。", distance=.9)
    vectors = Mock(search=Mock(return_value=[chunk]))
    embedding = Mock(embed=Mock(return_value=[[.1] * 1024]))
    rag = RagService(embedding, vectors, documents)
    def generate(*args, **kwargs):
        assert "806" in args[1]
        if callback:
            callback()
        return "验收编号为806。[Source 1]"
    llm = Mock(generate=Mock(side_effect=generate))
    return rag, RagAnswerService(rag, llm, documents), vectors, embedding, llm


def reclip(documents, doc):
    documents.transition_status(doc.id, "a", {DS.SUCCESS}, DS.PROCESSING)
    return documents.update_ingest_result(doc.id, chunk_count=1, status=DS.SUCCESS)


@pytest.mark.parametrize("scope", [None, "document"])
def test_api_discards_completed_reclip_even_with_identical_timestamp_and_chunk_count(store, scope, monkeypatch):
    engine, documents, doc = store
    fixed = datetime(2026, 1, 1)
    with engine.begin() as connection:
        connection.execute(text("UPDATE documents SET updated_at=:t WHERE id=:id"), {"t": fixed, "id": doc.id})
    def update():
        changed = reclip(documents, doc)
        with engine.begin() as connection:
            connection.execute(text("UPDATE documents SET updated_at=:t WHERE id=:id"), {"t": fixed, "id": doc.id})
        current = documents.get_document(doc.id, "a")
        assert current.updated_at == fixed and current.chunk_count == doc.chunk_count
        assert changed.content_revision > doc.content_revision
    _, service, _, _, llm = services(documents, doc, update)
    app = create_app()
    app.dependency_overrides[get_current_plugin] = lambda: SimpleNamespace(plugin_id="a")
    app.dependency_overrides[get_plugin_service] = lambda: Mock(decrypt_api_key=Mock(return_value="fixture"))
    app.dependency_overrides[get_rag_answer_service] = lambda: service
    monkeypatch.setattr("backend.main.get_milvus_initializer", lambda: Mock())
    with TestClient(app) as client:
        payload = {"query": "验收编号是多少？"}
        if scope:
            payload["document_id"] = doc.id
        response = client.post("/rag/ask", json=payload)
    assert response.status_code == 200
    assert response.json() == {"answer": "引用文档已更新，本次回答已丢弃，请重新提问。", "sources": []}
    llm.generate.assert_called_once()


@pytest.mark.parametrize("boundary", ["embedding", "retrieval"])
@pytest.mark.parametrize("scope", [None, "document"])
def test_reclip_during_retrieval_cannot_stamp_old_chunks_with_new_revision(store, boundary, scope):
    _, documents, doc = store
    rag, service, vectors, embedding, llm = services(documents, doc)
    dependency = embedding.embed if boundary == "embedding" else vectors.search
    original = dependency.return_value
    def change(*args, **kwargs):
        reclip(documents, doc)
        return original
    dependency.side_effect = change
    response = asyncio.run(service.ask("验收编号？", plugin_id="a", document_id=doc.id if scope else None))
    assert response.sources == [] and "806" not in response.answer
    llm.generate.assert_not_called()


def test_stable_revision_is_carried_internally_without_changing_search_json(store):
    _, documents, doc = store
    rag, service, _, _, llm = services(documents, doc)
    result = asyncio.run(rag.search("编号", plugin_id="a"))[0]
    assert result.content_revision == doc.content_revision
    assert "content_revision" not in result.model_dump()
    assert "content_revision" not in RagSearchResult.model_json_schema(mode="serialization")["properties"]
    response = asyncio.run(service.ask("编号", plugin_id="a"))
    assert "806" in response.answer and response.sources[0].document_id == doc.id
    llm.generate.assert_called_once()


@pytest.mark.parametrize("failure", [False, True])
def test_unavailable_snapshot_skips_paid_embedding(store, monkeypatch, failure):
    _, documents, doc = store
    rag, _, _, embedding, _ = services(documents, doc)
    def snapshot(*args):
        if failure:
            raise DocumentOperationError("snapshot unavailable")
        return {}
    monkeypatch.setattr(documents, "get_content_revisions", snapshot)
    if failure:
        with pytest.raises(DocumentOperationError):
            asyncio.run(rag.search("编号", plugin_id="a"))
    else:
        assert asyncio.run(rag.search("编号", plugin_id="a")) == []
    embedding.embed.assert_not_called()
