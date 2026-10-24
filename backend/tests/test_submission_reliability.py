"""Late submission errors must not overwrite work already owned by a worker."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

from backend.core.exceptions import IngestJobOperationError
from backend.api.deps import get_current_plugin
from backend.core.di import get_document_upload_service, get_ingest_job_service, get_plugin_service
from backend.main import create_app
from backend.models.base import Base
from backend.models.document import DocumentStatus as DS
from backend.models.document_api_schema import WebClipRequest
from backend.models.ingest_job import IngestJobStatus as JS, IngestJobType as JT
from backend.repositories.mysql import DocumentRepositoryImpl, IngestJobRepositoryImpl, PluginRepositoryImpl
from backend.services.document_upload import DocumentUploadService
from backend.services.ingest_job import IngestJobService


@pytest.fixture
def repos():
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    PluginRepositoryImpl(engine).create_plugin("a", "Test", "test", "a" * 64)
    yield DocumentRepositoryImpl(engine), IngestJobRepositoryImpl(engine)
    engine.dispose()


def upload_service(documents):
    return DocumentUploadService(documents, Mock(), Mock(), Mock(), Mock(),
                                 max_content_bytes=100, max_documents_per_workspace=10)


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("kind", [JT.WEB_CLIP, JT.FILE_UPLOAD])
@pytest.mark.parametrize("worker_state", [JS.RUNNING, JS.SUCCEEDED, JS.FAILED, JS.QUEUED])
def test_lost_enqueue_response_preserves_worker_state(repos, retry, kind, worker_state):
    documents, jobs = repos
    document = documents.create_document("note.txt", "note.txt", "a")
    queue = Mock()
    service = IngestJobService(jobs, queue, SimpleNamespace(ingest_max_retries=3, ingest_payload_ttl_seconds=600))
    observed = []

    def accepted_then_disconnected(job_id, *args, **kwargs):
        # Redis accepts the command and the worker advances before its reply
        # reaches the producer. QUEUED here means a later recovered attempt.
        job = jobs.claim_job(job_id)
        assert job is not None
        jobs.begin_document(job_id, document.id)
        if worker_state == JS.SUCCEEDED:
            documents.update_ingest_result(document.id, status=DS.SUCCESS, chunk_count=2)
            jobs.mark_succeeded(job_id, document.id)
        elif worker_state == JS.FAILED:
            jobs.mark_failed(job_id, "worker's actual error")
        elif worker_state == JS.QUEUED:
            jobs.recover_interrupted(job_id)
        observed.append(jobs.get_job(job_id, "a"))
        raise IngestJobOperationError("enqueue response lost")

    queue.enqueue.side_effect = accepted_then_disconnected
    queue.enqueue_existing.side_effect = accepted_then_disconnected
    if retry:
        body = {"job_type": kind, "plugin_id": "a", "url": "https://example.com", "raw_text": "body"}
        job = jobs.create_job("a", kind, document_id=document.id if kind == JT.FILE_UPLOAD else None,
                              payload=body if kind == JT.WEB_CLIP else None, payload_ttl_seconds=600)
        jobs.mark_failed(job.id, "previous submission failed")
        result = service.retry_job(job.id, "a")
    elif kind == JT.FILE_UPLOAD:
        result = service.create_file_upload_job("a", document.id)
    else:
        result = service.create_web_clip_job("a", WebClipRequest(url="https://example.com", raw_text="body"))

    before = observed[0]
    assert result.id == before.id
    after = jobs.get_job(before.id, "a")
    assert after.status == before.status == worker_state
    assert after.error_message == before.error_message
    assert after.attempt_count == before.attempt_count
    assert documents.get_document(document.id, "a").status == {
        JS.RUNNING: DS.PROCESSING, JS.SUCCEEDED: DS.SUCCESS,
        JS.FAILED: DS.FAILED, JS.QUEUED: DS.FAILED,
    }[worker_state]


@pytest.mark.parametrize("kind", [JT.WEB_CLIP, JT.FILE_UPLOAD])
def test_confirmed_publication_needs_no_fallible_readback(repos, monkeypatch, kind):
    documents, jobs = repos
    document = documents.create_document("note.txt", "note.txt", "a")
    service = IngestJobService(jobs, Mock(), SimpleNamespace(ingest_max_retries=3, ingest_payload_ttl_seconds=600))
    monkeypatch.setattr(jobs, "get_job", Mock(side_effect=IngestJobOperationError("read unavailable")))
    if kind == JT.FILE_UPLOAD:
        result = service.create_file_upload_job("a", document.id)
    else:
        result = service.create_web_clip_job("a", WebClipRequest(url="https://example.com", raw_text="body"))
    assert result.status == JS.QUEUED
    jobs.get_job.assert_not_called()


@pytest.mark.parametrize("state", list(DS.ALL))
def test_upload_route_compensation_only_changes_unreserved_pending_upload(repos, state):
    documents, _ = repos
    document = documents.create_document("note.txt", "note.txt", "a")
    documents.update_status(document.id, state)
    upload_service(documents).fail_prepared_upload(
        document.id, RuntimeError("submission failed"), plugin_id="a",
    )
    actual = documents.get_document(document.id, "a")
    assert actual.status == (DS.FAILED if state == DS.PENDING else state)
    assert actual.error_message == ("submission failed" if state == DS.PENDING else None)


@pytest.mark.parametrize("claimed", [False, True])
def test_upload_route_compensation_preserves_accepted_jobs(repos, claimed):
    documents, jobs = repos
    document = documents.create_document("note.txt", "note.txt", "a")
    job = jobs.create_job("a", JT.FILE_UPLOAD, document.id)
    if claimed:
        jobs.claim_job(job.id)
    upload_service(documents).fail_prepared_upload(
        document.id, RuntimeError("submission failed"), plugin_id="a",
    )
    assert documents.get_document(document.id, "a").status == DS.PENDING


def test_enqueue_outage_retains_durable_job_and_pending_upload(repos):
    documents, jobs = repos
    document = documents.create_document("note.txt", "note.txt", "a")
    queue = Mock(enqueue=Mock(side_effect=IngestJobOperationError("redis down")))
    service = IngestJobService(jobs, queue, SimpleNamespace(ingest_max_retries=3, ingest_payload_ttl_seconds=600))
    result = service.create_file_upload_job("a", document.id)
    job_id = queue.enqueue.call_args.args[0]
    assert result.id == job_id
    assert jobs.get_job(job_id, "a").status == JS.QUEUED
    assert documents.get_document(document.id, "a").status == DS.PENDING


def test_failed_compensation_rolls_back_job_and_document(repos, monkeypatch):
    documents, jobs = repos
    document = documents.create_document("note.txt", "note.txt", "a")
    job = jobs.create_job("a", JT.FILE_UPLOAD, document.id)
    release = jobs._release_document

    def interrupted_write(session, job, message):
        release(session, job, message)
        raise OperationalError("injected", {}, Exception("database down"))

    monkeypatch.setattr(jobs, "_release_document", interrupted_write)
    with pytest.raises(IngestJobOperationError, match="finish ingest job failed"):
        jobs.mark_failed(job.id, "submission failed", expected_attempt_count=0)
    assert jobs.get_job(job.id, "a").status == JS.QUEUED
    assert documents.get_document(document.id, "a").status == DS.PENDING


def test_upload_compensation_checks_ownership_and_source(repos):
    documents, _ = repos
    own = documents.create_document("note.txt", "note.txt", "a")
    webpage = documents.create_document("webclip.txt", "", "a", source_type="webpage")
    service = upload_service(documents)
    service.fail_prepared_upload(own.id, RuntimeError("error"), plugin_id="foreign")
    service.fail_prepared_upload(webpage.id, RuntimeError("error"), plugin_id="a")
    assert documents.get_document(own.id, "a").status == DS.PENDING
    assert documents.get_document(webpage.id, "a").status == DS.PENDING


@pytest.mark.parametrize("completed", [False, True])
def test_upload_api_lost_enqueue_response_does_not_break_worker(repos, monkeypatch, completed):
    documents, jobs = repos
    document = documents.create_document("note.txt", "note.txt", "a")
    upload = upload_service(documents)
    upload.prepare_upload = Mock(return_value=document)
    queue = Mock()

    def accepted_then_disconnected(job_id, payload):
        jobs.claim_job(job_id)
        jobs.begin_document(job_id, document.id)
        if completed:
            documents.update_ingest_result(document.id, status=DS.SUCCESS, chunk_count=2)
            jobs.mark_succeeded(job_id, document.id)
        raise IngestJobOperationError("enqueue response lost")

    queue.enqueue.side_effect = accepted_then_disconnected
    app = create_app()
    monkeypatch.setattr("backend.main.get_milvus_initializer", lambda: Mock())
    app.dependency_overrides[get_current_plugin] = lambda: SimpleNamespace(plugin_id="a")
    app.dependency_overrides[get_plugin_service] = lambda: Mock()
    app.dependency_overrides[get_document_upload_service] = lambda: upload
    app.dependency_overrides[get_ingest_job_service] = lambda: IngestJobService(
        jobs, queue, SimpleNamespace(ingest_max_retries=3, ingest_payload_ttl_seconds=600),
    )
    with TestClient(app) as client:
        response = client.post("/documents/upload/async", files={"file": ("note.txt", b"body", "text/plain")})
    assert response.status_code == 202
    assert response.json()["status"] == JS.QUEUED
    job_id = queue.enqueue.call_args.args[0]
    assert jobs.get_job(job_id, "a").status == (JS.SUCCEEDED if completed else JS.RUNNING)
    assert documents.get_document(document.id, "a").status == (DS.SUCCESS if completed else DS.PROCESSING)
    if not completed:
        # Its reservation is intact; the worker can still commit success.
        documents.update_ingest_result(document.id, status=DS.SUCCESS, chunk_count=2)
        jobs.mark_succeeded(job_id, document.id)
    assert jobs.get_job(job_id, "a").status == JS.SUCCEEDED
