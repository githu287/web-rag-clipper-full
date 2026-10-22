"""Regression coverage for quota admission, durable task lookup and chunk limits."""
import asyncio
from datetime import datetime, timedelta
from random import Random
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, update
from sqlalchemy.pool import StaticPool

from backend.api.deps import get_current_plugin
from backend.chunkers import RecursiveCharacterChunker
from backend.core.config import Settings
from backend.core.di import get_document_repository, get_ingest_job_service, get_plugin_service, get_web_clip_service
from backend.core.exceptions import DocumentQuotaExceededError, IngestJobNotFoundError
from backend.main import create_app
from backend.models.base import Base
from backend.models.document import Document, DocumentStatus
from backend.models.ingest_job import IngestJob, IngestJobStatus, IngestJobType
from backend.repositories.mysql.impl import DocumentRepositoryImpl
from backend.repositories.mysql.ingest_job_impl import IngestJobRepositoryImpl
from backend.repositories.mysql.plugin_impl import PluginRepositoryImpl
from backend.services.document_upload import DocumentUploadService
from backend.services.ingest_job import IngestJobService
from backend.services.web_clip import WebClipService


@pytest.fixture
def repos():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    plugins = PluginRepositoryImpl(engine)
    for owner in ("owner-a", "owner-b"):
        plugins.create_plugin(owner, owner, owner, owner[-1] * 64)
    yield engine, DocumentRepositoryImpl(engine, max_documents_per_workspace=2), IngestJobRepositoryImpl(engine)
    engine.dispose()


def clip_service(documents):
    chunker = RecursiveCharacterChunker()
    ingest = Mock()
    ingest.ingest_document = AsyncMock()
    return WebClipService(documents, chunker, ingest, max_documents_per_workspace=2)


def client_for(documents, jobs=None, clip=None):
    app = create_app()
    app.dependency_overrides[get_current_plugin] = lambda: SimpleNamespace(plugin_id="owner-a")
    app.dependency_overrides[get_document_repository] = lambda: documents
    if jobs:
        app.dependency_overrides[get_ingest_job_service] = lambda: jobs
    if clip:
        app.dependency_overrides[get_web_clip_service] = lambda: clip
        app.dependency_overrides[get_plugin_service] = lambda: Mock()
    return TestClient(app)


@pytest.mark.parametrize("status", sorted(DocumentStatus.ALL))
def test_metadata_api_enforces_all_status_quota_and_workspace_isolation(repos, status):
    _, documents, _ = repos
    doc = documents.create_document("one", "", "owner-a")
    documents.update_status(doc.id, status)
    documents.create_document("two", "", "owner-a")
    with client_for(documents) as client:
        response = client.post("/documents", json={"filename": "excess"})
    assert response.status_code == 429
    assert response.json()["type"] == "DocumentQuotaExceededError"
    assert documents.count_documents("owner-a") == 2
    assert documents.create_document("another workspace", "", "owner-b").id


@pytest.mark.parametrize("suffix", ["", "?utm_source=extension#section"])
def test_full_async_library_accepts_existing_normalized_url_but_rejects_new(repos, suffix):
    _, documents, job_repo = repos
    doc = documents.create_document("webclip.txt", "", "owner-a", url="https://example.com/a", source_type="webpage")
    documents.create_document("two", "", "owner-a")
    queue = Mock()
    service = IngestJobService(job_repo, queue, Settings())
    with client_for(documents, service, clip_service(documents)) as client:
        accepted = client.post("/clips/async", json={"url": "https://example.com/a" + suffix, "raw_text": "new body"})
        rejected = client.post("/clips/async", json={"url": "https://example.com/new", "raw_text": "body"})
    assert accepted.status_code == 202
    assert rejected.status_code == 429
    assert documents.count_documents("owner-a") == 2
    queue.ensure_enqueued.assert_called_once()
    assert job_repo.get_job(accepted.json()["id"], "owner-a").payload["raw_text"] == "new body"
    # Worker re-clip actually reuses the full workspace's document as well.
    result = asyncio.run(clip_service(documents).clip("https://example.com/a" + suffix, "new body", "owner-a"))
    assert result.id == doc.id
    assert documents.count_documents("owner-a") == 2


def test_another_workspace_url_does_not_bypass_full_admission(repos):
    _, documents, _ = repos
    documents.create_document("other", "", "owner-b", url="https://example.com/a", source_type="webpage")
    for name in ("one", "two"):
        documents.create_document(name, "", "owner-a")
    with pytest.raises(DocumentQuotaExceededError):
        clip_service(documents).ensure_capacity("owner-a", url="https://example.com/a")


def test_upload_cleans_file_if_final_atomic_quota_check_loses_race(repos):
    _, documents, _ = repos
    documents.create_document("one", "", "owner-a")
    storage = Mock()

    def competing_save(filename, content):
        documents.create_document("concurrent winner", "", "owner-a")
        return "new-upload.txt"

    storage.save.side_effect = competing_save
    upload = DocumentUploadService(documents, storage, Mock(), Mock(), Mock(), 2048, 2)
    with pytest.raises(DocumentQuotaExceededError):
        upload.prepare_upload("notes.txt", b"body", "owner-a", "text/plain")
    storage.delete.assert_called_once_with("new-upload.txt")
    assert documents.count_documents("owner-a") == 2


def test_job_lookup_recovers_without_local_record_and_hides_payload(repos):
    _, documents, job_repo = repos
    doc = documents.create_document("webclip.txt", "", "owner-a", source_type="webpage")
    job = job_repo.create_job("owner-a", IngestJobType.WEB_CLIP, document_id=doc.id,
                              payload={"raw_text": "private source body", "plugin_id": "owner-a", "job_type": "WEB_CLIP"})
    job_repo.mark_failed(job.id, "synthetic failure")
    queue = Mock()
    jobs = IngestJobService(job_repo, queue, Settings())
    with client_for(documents, jobs) as client:
        result = client.get(f"/documents/{doc.id}/job")
        assert result.status_code == 200
        assert result.json()["id"] == job.id
        assert "private source body" not in result.text
        assert "payload" not in result.json()
        retried = client.post(f"/jobs/{job.id}/retry")
        assert retried.status_code == 202
        assert retried.json()["id"] == job.id
        assert retried.json()["status"] == "QUEUED"
    queue.ensure_enqueued.assert_called_once()


def test_document_job_lookup_never_leaks_other_owners_or_deleted_documents(repos):
    _, documents, job_repo = repos
    doc = documents.create_document("other", "", "owner-b")
    job_repo.create_job("owner-b", IngestJobType.FILE_UPLOAD, document_id=doc.id)
    with pytest.raises(IngestJobNotFoundError):
        job_repo.get_document_job(doc.id, "owner-a")
    # Even a malformed legacy cross-owner job cannot authorize a foreign document.
    job_repo.create_job("owner-a", IngestJobType.FILE_UPLOAD, document_id=doc.id)
    with pytest.raises(IngestJobNotFoundError):
        job_repo.get_document_job(doc.id, "owner-a")
    documents.delete_document(doc.id, "owner-b")
    with pytest.raises(IngestJobNotFoundError):
        job_repo.get_document_job(doc.id, "owner-b")
    with client_for(documents, IngestJobService(job_repo, Mock(), Settings())) as client:
        assert client.get(f"/documents/{doc.id}/job").status_code == 404
        assert client.get("/documents/99999/job").status_code == 404


@pytest.mark.parametrize("new_status", [IngestJobStatus.SUCCEEDED, IngestJobStatus.QUEUED, IngestJobStatus.RUNNING])
def test_lookup_does_not_offer_stale_failure_after_newer_work(repos, new_status):
    engine, documents, job_repo = repos
    doc = documents.create_document("one", "", "owner-a")
    old = job_repo.create_job("owner-a", IngestJobType.FILE_UPLOAD, document_id=doc.id)
    job_repo.mark_failed(old.id, "old failure")
    newer = job_repo.create_job("owner-a", IngestJobType.FILE_UPLOAD, document_id=doc.id)
    with engine.begin() as connection:
        connection.execute(update(IngestJob).where(IngestJob.id == old.id).values(created_at=datetime(2026, 1, 1)))
        connection.execute(update(IngestJob).where(IngestJob.id == newer.id).values(
            status=new_status, created_at=datetime(2026, 1, 1) + timedelta(seconds=1)))
    assert job_repo.get_document_job(doc.id, "owner-a").id == newer.id


def test_active_job_wins_over_newer_preflight_failure(repos):
    _, documents, job_repo = repos
    doc = documents.create_document("one", "", "owner-a")
    active = job_repo.create_job("owner-a", IngestJobType.FILE_UPLOAD, document_id=doc.id)
    failed = job_repo.create_job("owner-a", IngestJobType.FILE_UPLOAD, document_id=doc.id)
    job_repo.mark_failed(failed.id, "another job still running")
    assert job_repo.get_document_job(doc.id, "owner-a").id == active.id


def test_paragraph_overlap_cannot_overflow_chunk_size():
    text = "a" * 650 + "\n\n" + "b" * 650
    chunks = RecursiveCharacterChunker(700, 100).split(text)
    assert max(map(len, chunks)) == 700
    assert chunks == ["a" * 650 + "\n\n", "a" * 48 + "\n\n" + "b" * 650]


def test_chunk_boundaries_preserve_source_across_separator_sizes():
    rng = Random(20261022)
    separators = ("\n\n", "\n", "。", "！", "；", "，", " ", "")
    for size in (1, 2, 7, 31, 100, 700):
        for overlap in sorted({0, size // 2, size - 1}):
            for separator in separators:
                text = separator.join("".join(chr(0x4E00 + n * 1000 + i) for i in range(rng.randint(1, size + 3))) for n in range(4))
                chunks = RecursiveCharacterChunker(size, overlap).split(text)
                assert all(0 < len(chunk) <= size for chunk in chunks)
                # Each chunk is a source substring; no content is skipped or reordered.
                covered = 0
                for chunk in chunks:
                    start = text.find(chunk, max(0, covered - len(chunk) + 1))
                    assert 0 <= start <= covered
                    covered = max(covered, start + len(chunk))
                assert covered == len(text)
                if overlap == 0:
                    assert "".join(chunks) == text
