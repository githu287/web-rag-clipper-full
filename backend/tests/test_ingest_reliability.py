"""Cross-service regressions using real persistence and fake external services."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from backend.api.deps import get_current_plugin
from backend.core.di import get_document_ingest_service, get_plugin_service
from backend.core.exceptions import DocumentOperationError, IngestJobConflictError
from backend.main import create_app
from backend.models.base import Base
from backend.models.document import DocumentStatus as DS
from backend.models.ingest_job import IngestJobStatus as JS, IngestJobType
from backend.models.plugin import PluginWorkspace
from backend.repositories.mysql.impl import DocumentRepositoryImpl
from backend.repositories.mysql.ingest_job_impl import IngestJobRepositoryImpl
from backend.services.document_delete import DocumentDeleteService
from backend.services.document_ingest import DocumentIngestService
from backend.services.rag_answer import RagAnswerService
from backend.services.web_clip import WebClipService


@pytest.fixture
def repos():
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        for name in ("a", "b"):
            connection.execute(PluginWorkspace.__table__.insert().values(
                plugin_id=name, plugin_name=name, plugin_name_norm=name,
                plugin_secret_hash=name * 64, status="ACTIVE",
            ))
    yield DocumentRepositoryImpl(engine), IngestJobRepositoryImpl(engine)
    engine.dispose()


def make_document(documents, plugin="a"):
    return documents.create_document("webclip.txt", "", plugin,
                                     source_type="webpage", url="https://example.com/")


def test_raw_ingest_cannot_overwrite_foreign_or_missing_document(repos):
    documents, _ = repos
    document = make_document(documents, "b")
    vectors = SimpleNamespace(ingest_page=AsyncMock())
    service = DocumentIngestService(documents, vectors)
    app = create_app()
    app.dependency_overrides[get_current_plugin] = lambda: SimpleNamespace(plugin_id="a")
    app.dependency_overrides[get_plugin_service] = lambda: Mock(decrypt_api_key=Mock(return_value="test-key"))
    app.dependency_overrides[get_document_ingest_service] = lambda: service
    with TestClient(app, raise_server_exceptions=True) as client:
        for target in (document.id, document.id + 999):
            assert client.post("/ingest/page", json={"page_id": target, "chunks": ["replacement"]}).status_code == 404
    vectors.ingest_page.assert_not_called()
    assert documents.get_document(document.id, "b").status == DS.PENDING


@pytest.fixture(autouse=True)
def no_milvus_startup(monkeypatch):
    monkeypatch.setattr("backend.main.get_milvus_initializer", lambda: Mock())


def test_ingest_and_delete_cannot_both_claim_document(repos):
    documents, _ = repos
    document = make_document(documents)
    documents.transition_status(document.id, "a", {DS.PENDING}, DS.PROCESSING)
    with pytest.raises(DocumentOperationError):
        documents.transition_status(document.id, "a", {DS.PENDING}, DS.DELETING)
    assert documents.get_document(document.id, "a").status == DS.PROCESSING


def test_job_document_claim_rolls_back_on_foreign_or_busy_document(repos):
    documents, jobs = repos
    foreign = make_document(documents, "b")
    job = jobs.create_job("a", IngestJobType.WEB_CLIP)
    jobs.claim_job(job.id)
    with pytest.raises(IngestJobConflictError):
        jobs.begin_document(job.id, foreign.id)
    assert jobs.get_job(job.id, "a").document_id is None
    assert documents.get_document(foreign.id, "b").status == DS.PENDING
    own = make_document(documents)
    documents.transition_status(own.id, "a", {DS.PENDING}, DS.DELETING)
    with pytest.raises(IngestJobConflictError):
        jobs.begin_document(job.id, own.id)
    assert documents.get_document(own.id, "a").status == DS.DELETING


def test_interrupted_web_clip_can_resume_same_document(repos):
    documents, jobs = repos
    chunker = Mock(split=Mock(return_value=["body"]))
    vectors = SimpleNamespace(ingest_page=AsyncMock(side_effect=KeyboardInterrupt()))
    clip = WebClipService(
        documents,
        chunker,
        DocumentIngestService(documents, vectors),
        max_documents_per_workspace=5,
    )
    job = jobs.create_job("a", IngestJobType.WEB_CLIP)
    jobs.claim_job(job.id)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(clip.clip("https://example.com/", "body", "a", api_key="test",
                             claim_document=lambda doc_id: jobs.begin_document(job.id, doc_id)))
    interrupted = jobs.get_job(job.id, "a")
    assert interrupted.document_id is not None
    assert documents.get_document(interrupted.document_id, "a").status == DS.PROCESSING
    assert jobs.recover_interrupted(job.id)
    assert documents.get_document(interrupted.document_id, "a").status == DS.FAILED
    jobs.claim_job(job.id)
    vectors.ingest_page.side_effect = None
    result = asyncio.run(clip.clip("https://example.com/", "body", "a", api_key="test",
                                  claim_document=lambda doc_id: jobs.begin_document(job.id, doc_id)))
    jobs.mark_succeeded(job.id, result.id)
    assert result.id == interrupted.document_id
    assert result.status == DS.SUCCESS
    assert jobs.get_job(job.id, "a").status == JS.SUCCEEDED


def test_successful_document_reserved_until_job_finishes(repos):
    documents, jobs = repos
    doc = make_document(documents)
    job = jobs.create_job("a", IngestJobType.WEB_CLIP)
    jobs.claim_job(job.id)
    jobs.begin_document(job.id, doc.id)
    documents.update_ingest_result(doc.id, chunk_count=1, status=DS.SUCCESS)
    with pytest.raises(DocumentOperationError):
        documents.transition_status(doc.id, "a", {DS.SUCCESS}, DS.PROCESSING)
    jobs.mark_succeeded(job.id, doc.id)
    documents.transition_status(doc.id, "a", {DS.SUCCESS}, DS.PROCESSING)


def test_failed_job_releases_only_its_claimed_document(repos):
    documents, jobs = repos
    doc = make_document(documents)
    owner = jobs.create_job("a", IngestJobType.WEB_CLIP)
    competitor = jobs.create_job("a", IngestJobType.WEB_CLIP)
    jobs.claim_job(owner.id)
    jobs.begin_document(owner.id, doc.id)
    jobs.claim_job(competitor.id)
    with pytest.raises(IngestJobConflictError):
        jobs.begin_document(competitor.id, doc.id)
    jobs.mark_failed(competitor.id, "busy")
    assert documents.get_document(doc.id, "a").status == DS.PROCESSING
    jobs.mark_failed(owner.id, "failed")
    assert documents.get_document(doc.id, "a").status == DS.FAILED


def test_enqueue_failure_can_finish_queued_job(repos):
    _, jobs = repos
    job = jobs.create_job("a", IngestJobType.WEB_CLIP)
    assert jobs.mark_failed(job.id, "redis unavailable").status == JS.FAILED
    with pytest.raises(IngestJobConflictError):
        jobs.mark_failed(job.id, "duplicate")


def test_upload_preflight_failure_does_not_leave_pending_document(repos):
    documents, jobs = repos
    doc = documents.create_document("note.md", "note.md", "a", source_type="upload")
    job = jobs.create_job("a", IngestJobType.FILE_UPLOAD, doc.id)
    jobs.claim_job(job.id)
    jobs.mark_failed(job.id, "credentials unavailable")
    assert documents.get_document(doc.id, "a").status == DS.FAILED


def test_partial_delete_stays_deleting_until_retry(repos):
    documents, _ = repos
    doc = make_document(documents)
    documents.update_ingest_result(doc.id, chunk_count=1, status=DS.SUCCESS)
    milvus = Mock(query_page_chunks=Mock(return_value=[f"{doc.id}_0"]),
                  delete_chunks=Mock(side_effect=RuntimeError("partial delete")))
    service = DocumentDeleteService(documents, milvus, Mock())
    with pytest.raises(RuntimeError):
        asyncio.run(service.delete_document(doc.id, "a"))
    assert documents.get_document(doc.id, "a").status == DS.DELETING
    assert documents.get_success_document_ids("a") == []
    milvus.delete_chunks.side_effect = None
    asyncio.run(service.delete_document(doc.id, "a"))
    assert documents.count_documents("a") == 0


def test_slow_llm_does_not_block_event_loop():
    started, release = threading.Event(), threading.Event()
    def generate(*args, **kwargs):
        started.set()
        if not release.wait(2):
            raise RuntimeError("event loop was blocked")
        return "answer"
    result = SimpleNamespace(title="title", url="https://example.com", document_id=1,
                             chunk_text="body", id="1_0", page_id=1, distance=0.9)
    service = RagAnswerService(SimpleNamespace(search=AsyncMock(return_value=[result])),
                               SimpleNamespace(generate=generate), Mock())
    async def scenario():
        task = asyncio.create_task(service.ask("question", plugin_id="a", api_key="test"))
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()
        finally:
            release.set()
        assert (await task).answer == "answer"
    asyncio.run(scenario())
