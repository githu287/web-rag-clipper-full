"""Real persistence with a Redis boundary model; no personal data or services."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, event, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

from backend.core.exceptions import IngestJobConflictError, IngestJobOperationError
from backend.models.base import Base
from backend.models.document import DocumentStatus as DS
from backend.models.document_api_schema import WebClipRequest
from backend.models.ingest_job import IngestJob, IngestJobStatus as JS, IngestJobType as JT
from backend.repositories.mysql import DocumentRepositoryImpl, IngestJobRepositoryImpl, PluginRepositoryImpl
from backend.services.ingest_job import IngestJobService
from backend.workers.ingest_worker import IngestWorker


class MemoryQueue:
    """Model atomic publication, unacknowledged delivery, and total Redis loss."""
    def __init__(self):
        self.pending, self.processing, self.payloads = [], [], {}
        self.offline = False

    def check(self):
        if self.offline:
            raise IngestJobOperationError("Redis unavailable")

    def ensure_enqueued(self, job_id, payload, *, ttl_seconds=None):
        return self.enqueue(job_id, payload, ttl_seconds=ttl_seconds)

    def enqueue(self, job_id, payload, *, ttl_seconds=None):
        self.check()
        self.payloads.setdefault(job_id, payload)
        if job_id not in self.pending and job_id not in self.processing:
            self.pending.insert(0, job_id)
            return True
        return False

    def enqueue_existing(self, job_id):
        self.check()
        if job_id not in self.payloads:
            raise IngestJobOperationError("ingest job payload has expired")
        self.enqueue(job_id, self.payloads[job_id])

    def dequeue(self, timeout_seconds):
        self.check()
        if not self.pending:
            return None
        job_id = self.pending.pop()
        self.processing.insert(0, job_id)
        return job_id

    def list_processing(self):
        self.check()
        return list(self.processing)

    def recover_processing(self):
        self.check()
        while self.processing:
            self.pending.insert(0, self.processing.pop())

    def get_payload(self, job_id):
        self.check()
        return self.payloads.get(job_id)

    def delete_payload(self, job_id):
        self.check()
        self.payloads.pop(job_id, None)

    def acknowledge(self, job_id):
        self.check()
        self.processing.remove(job_id)

    def lose_all_data(self):
        self.pending.clear()
        self.processing.clear()
        self.payloads.clear()


@pytest.fixture
def system():
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    plugins, documents, jobs = PluginRepositoryImpl(engine), DocumentRepositoryImpl(engine), IngestJobRepositoryImpl(engine)
    plugins.create_plugin("a", "Test", "test", "a" * 64)
    queue = MemoryQueue()
    calls = []

    async def clip(**kwargs):
        doc = documents.get_webpage_by_url("a", kwargs["url"])
        if doc is None:
            doc = documents.create_document("webclip.txt", "", "a", source_type="webpage", url=kwargs["url"])
        return await process(document_id=doc.id, **kwargs)

    async def process(document_id, **kwargs):
        kwargs["claim_document"](document_id)
        calls.append(document_id)
        return documents.update_ingest_result(document_id, status=DS.SUCCESS, chunk_count=1)

    service = IngestJobService(jobs, queue, SimpleNamespace(ingest_max_retries=3, ingest_payload_ttl_seconds=600))
    worker = IngestWorker(jobs, queue, plugins, Mock(decrypt_api_key=Mock(return_value="test-key")),
                           SimpleNamespace(clip=clip), SimpleNamespace(process_upload=process), max_retries=3)
    yield SimpleNamespace(engine=engine, documents=documents, jobs=jobs, plugins=plugins,
                          queue=queue, service=service, worker=worker, calls=calls)
    engine.dispose()


def submit(system, kind):
    if kind == JT.FILE_UPLOAD:
        doc = system.documents.create_document("note.txt", "note.txt", "a")
        return system.service.create_file_upload_job("a", doc.id)
    return system.service.create_web_clip_job("a", WebClipRequest(url="https://example.com/", raw_text="body"))


@pytest.mark.parametrize("kind", list(JT.ALL))
def test_redis_outage_accepts_job_and_worker_later_completes_it(system, kind):
    system.queue.offline = True
    job = submit(system, kind)
    assert job.status == JS.QUEUED
    assert system.jobs.get_job(job.id, "a").attempt_count == 0
    system.queue.offline = False
    assert system.worker.run_once(0)
    assert system.jobs.get_job(job.id, "a").status == JS.SUCCEEDED
    assert not system.worker.run_once(0)
    assert len(system.calls) == 1


@pytest.mark.parametrize("kind", list(JT.ALL))
@pytest.mark.parametrize("retry", [False, True])
def test_process_exit_after_database_commit_is_recoverable(system, monkeypatch, kind, retry):
    if retry:
        initial = submit(system, kind)
        system.jobs.mark_failed(initial.id, "previous failure")
        system.queue.lose_all_data()

    def exit_before_publication(*args, **kwargs):
        raise SystemExit("injected process exit")

    original = system.queue.enqueue
    monkeypatch.setattr(system.queue, "enqueue", exit_before_publication)
    with pytest.raises(SystemExit):
        if retry:
            system.service.retry_job(initial.id, "a")
        else:
            submit(system, kind)
    queued = system.jobs.list_queued()
    assert len(queued) == 1
    monkeypatch.setattr(system.queue, "enqueue", original)
    assert system.worker.run_once(0)
    assert system.jobs.get_job(queued[0].id, "a").status == JS.SUCCEEDED
    assert len(system.calls) == 1


@pytest.mark.parametrize("kind", list(JT.ALL))
@pytest.mark.parametrize("document_completed", [False, True])
def test_total_redis_loss_recovers_running_job_from_database(system, kind, document_completed):
    job = submit(system, kind)
    doc_id = job.document_id
    if doc_id is None:
        doc_id = system.documents.create_document("webclip.txt", "", "a", source_type="webpage",
                                                   url="https://example.com/").id
    system.jobs.claim_job(job.id)
    system.jobs.begin_document(job.id, doc_id)
    if document_completed:
        system.documents.update_ingest_result(doc_id, status=DS.SUCCESS, chunk_count=1)
    system.queue.lose_all_data()
    system.worker.run_once(0)
    final = system.jobs.get_job(job.id, "a")
    assert final.status == JS.SUCCEEDED
    assert final.attempt_count == (1 if document_completed else 2)
    assert len(system.calls) == (0 if document_completed else 1)


def test_reconciliation_is_bounded_and_does_not_duplicate_delivery(system):
    system.queue.offline = True
    jobs = [submit(system, JT.WEB_CLIP) for _ in range(23)]
    system.queue.offline = False
    assert system.worker.recover_queued() == 20
    assert system.worker.recover_queued() == 0
    assert len(system.queue.pending) == 20
    for _ in jobs:
        assert system.worker.run_once(0)
    assert not system.jobs.list_queued()
    assert len(system.calls) == 23


def expire(system, job_id):
    with system.engine.begin() as connection:
        connection.execute(update(IngestJob).where(IngestJob.id == job_id).values(
            payload_expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1),
        ))


def test_expired_web_body_is_purged_failed_and_never_resurrected(system):
    job = submit(system, JT.WEB_CLIP)
    expire(system, job.id)
    system.queue.lose_all_data()
    system.worker.run_once(0)
    final = system.jobs.get_job(job.id, "a")
    assert final.status == JS.FAILED
    assert final.payload is None
    assert final.payload_expires_at is not None
    assert system.queue.payloads == {}
    assert system.calls == []
    with pytest.raises(IngestJobConflictError, match="expired"):
        system.service.retry_job(job.id, "a")


def test_payload_cleanup_advances_through_batches_and_clears_success(system):
    jobs = [submit(system, JT.WEB_CLIP) for _ in range(5)]
    for job in jobs:
        system.jobs.mark_failed(job.id, "failed")
        expire(system, job.id)
    assert [system.jobs.purge_expired_payloads(2) for _ in range(4)] == [2, 2, 1, 0]
    fresh = submit(system, JT.WEB_CLIP)
    # Old terminal deliveries are acknowledged before the fresh FIFO item.
    for _ in range(6):
        system.worker.run_once(0)
    final = system.jobs.get_job(fresh.id, "a")
    assert final.status == JS.SUCCEEDED
    assert final.payload is final.payload_expires_at is None
    assert fresh.id not in system.queue.payloads


def test_large_unicode_body_is_persisted_and_not_exposed_in_job_response(system):
    from backend.api.routers.jobs import build_job_response
    raw_text = "内容" * 40000
    system.queue.offline = True
    job = system.service.create_web_clip_job("a", WebClipRequest(url="https://example.com/", raw_text=raw_text))
    saved = system.jobs.get_job(job.id, "a")
    assert saved.payload["raw_text"] == raw_text
    assert len(raw_text.encode("utf-8")) > 65535
    assert "payload" not in build_job_response(saved).model_dump()
    assert "test-key" not in str(saved.payload)

    # Status polling must not read the potentially large web body from the DB.
    queries = []
    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        queries.append(statement)
    event.listen(system.engine, "before_cursor_execute", capture)
    try:
        response = build_job_response(system.service.get_job(job.id, "a"))
        assert response.id == job.id
        assert all("ingest_jobs.payload," not in sql for sql in queries)
    finally:
        event.remove(system.engine, "before_cursor_execute", capture)


def test_workspace_removal_cascades_durable_payload(system):
    job = submit(system, JT.WEB_CLIP)
    system.plugins.delete_plugin("a")
    assert system.jobs.list_queued() == []
    assert system.jobs.claim_job(job.id) is None


def test_legacy_web_job_missing_body_becomes_observable_failure(system):
    job = system.jobs.create_job("a", JT.WEB_CLIP)
    system.worker.run_once(0)
    final = system.jobs.get_job(job.id, "a")
    assert final.status == JS.FAILED
    assert "missing or expired" in final.error_message


def test_retry_and_reconciliation_keep_original_deadline(system, monkeypatch):
    job = submit(system, JT.WEB_CLIP)
    deadline = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=120)
    with system.engine.begin() as connection:
        connection.execute(update(IngestJob).where(IngestJob.id == job.id).values(payload_expires_at=deadline))
    system.jobs.mark_failed(job.id, "previous failure")
    system.queue.lose_all_data()
    publication = Mock(wraps=system.queue.enqueue)
    monkeypatch.setattr(system.queue, "enqueue", publication)
    system.service.retry_job(job.id, "a")
    system.worker.recover_queued()
    assert system.jobs.get_job(job.id, "a").payload_expires_at == deadline
    assert all(0 < call.kwargs["ttl_seconds"] <= 120 for call in publication.call_args_list)


@pytest.mark.parametrize("change", ["expired", "limit"])
def test_retry_rechecks_deadline_and_attempt_limit_in_database(system, monkeypatch, change):
    job = submit(system, JT.WEB_CLIP)
    system.jobs.mark_failed(job.id, "previous failure")
    original = system.jobs.get_job

    def stale_snapshot(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        with system.engine.begin() as connection:
            connection.execute(update(IngestJob).where(IngestJob.id == job.id).values(
                **({"attempt_count": 3} if change == "limit" else {
                    "payload_expires_at": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1),
                }),
            ))
        return snapshot

    monkeypatch.setattr(system.jobs, "get_job", stale_snapshot)
    with pytest.raises(IngestJobConflictError, match=change):
        system.service.retry_job(job.id, "a")
    assert original(job.id, "a").status == JS.FAILED


@pytest.mark.parametrize("committed", [False, True])
def test_database_commit_failure_is_atomic_or_recoverable(system, monkeypatch, committed):
    factory = system.jobs._session_factory

    def uncertain_session():
        session = factory()
        commit = session.commit
        def fail_commit():
            session.flush()
            if committed:
                commit()
            raise OperationalError("injected", {}, Exception("commit response lost"))
        session.commit = fail_commit
        return session

    monkeypatch.setattr(system.jobs, "_session_factory", uncertain_session)
    with pytest.raises(IngestJobOperationError, match="create ingest job failed"):
        submit(system, JT.WEB_CLIP)
    monkeypatch.setattr(system.jobs, "_session_factory", factory)
    queued = system.jobs.list_queued()
    assert len(queued) == int(committed)
    assert system.queue.payloads == {}
    if committed:
        assert queued[0].payload["raw_text"] == "body"
        system.worker.run_once(0)
        assert system.jobs.get_job(queued[0].id, "a").status == JS.SUCCEEDED
