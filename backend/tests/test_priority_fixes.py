"""Regression tests for file ownership, durable delivery and model-space races."""
import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from backend.api.deps import get_current_plugin
from backend.core.config import Settings
from backend.core.di import get_document_delete_service, get_document_repository
from backend.core.exceptions import ApiKeyValidationError, DocumentOperationError
from backend.core.security import encrypt_api_key
from backend.main import create_app
from backend.models.base import Base
from backend.models.document import DocumentStatus
from backend.models.document_api_schema import WebClipRequest
from backend.models.ingest_job import IngestJobType
from backend.models.model_provider import (
    WorkspaceModelCredentials, embedding_config_fingerprint, legacy_dashscope_credentials,
)
from backend.repositories.mysql.impl import DocumentRepositoryImpl
from backend.repositories.mysql.ingest_job_impl import IngestJobRepositoryImpl
from backend.repositories.mysql.plugin_impl import PluginRepositoryImpl
from backend.services.document_delete import DocumentDeleteService
from backend.services.document_ingest import DocumentIngestService
from backend.services.ingest_job import IngestJobService
from backend.services.plugin_service import PluginService
from backend.services.rag import RagService
from backend.services.web_clip import WebClipService
from backend.storage.local import LocalFileStorage
from backend.workers.ingest_worker import IngestWorker


OLD = legacy_dashscope_credentials("synthetic-key")
NEW = WorkspaceModelCredentials(replace(OLD.embedding, model="another-model"), OLD.llm)
OLD_FP = embedding_config_fingerprint(OLD.embedding)
NEW_FP = embedding_config_fingerprint(NEW.embedding)


@pytest.fixture
def repositories():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    plugins = PluginRepositoryImpl(engine)
    for plugin_id, digest in [("workspace-a", "a"), ("workspace-b", "b")]:
        plugins.create_plugin(plugin_id, plugin_id, plugin_id, digest * 64)
        plugins.update_api_key(plugin_id, "synthetic-ciphertext", "synthetic-nonce", OLD_FP)
    yield DocumentRepositoryImpl(engine), IngestJobRepositoryImpl(engine), plugins
    engine.dispose()


@pytest.fixture
def storage_path():
    with TemporaryDirectory(prefix="web-rag-priority-") as directory:
        yield Path(directory)


def test_public_metadata_cannot_claim_another_workspace_file(repositories, storage_path):
    documents, _, _ = repositories
    storage = LocalFileStorage(str(storage_path))
    key = storage.save("victim.txt", b"synthetic content")
    documents.create_document("victim.txt", key, "workspace-b")
    app = create_app()
    app.dependency_overrides[get_current_plugin] = lambda: SimpleNamespace(plugin_id="workspace-a")
    app.dependency_overrides[get_document_repository] = lambda: documents
    milvus = Mock()
    milvus.query_page_chunks.return_value = []
    app.dependency_overrides[get_document_delete_service] = lambda: DocumentDeleteService(documents, milvus, storage)
    client = TestClient(app)
    try:
        response = client.post("/documents", json={"filename": "alias.txt", "file_path": key})
        assert response.status_code == 422
        assert documents.count_documents("workspace-a") == 0
        assert Path(storage.resolve(key)).exists()
        # Virtual metadata still supports the create -> ingest compatibility path.
        created = client.post("/documents", json={"filename": "virtual.txt"})
        assert created.status_code == 201
        assert created.json()["file_path"] == ""
        assert client.delete(f"/documents/{created.json()['id']}").status_code == 204
        assert Path(storage.resolve(key)).exists()
    finally:
        client.close()


def test_legacy_ambiguous_file_reference_cannot_delete_either_owners_file(repositories, storage_path):
    documents, _, _ = repositories
    storage = LocalFileStorage(str(storage_path))
    key = storage.save("victim.txt", b"synthetic content")
    victim = documents.create_document("victim.txt", key, "workspace-b")
    alias = documents.create_document("alias.txt", key, "workspace-a")
    milvus = Mock()
    milvus.query_page_chunks.return_value = []
    service = DocumentDeleteService(documents, milvus, storage)
    for doc, owner in [(alias, "workspace-a"), (victim, "workspace-b")]:
        with pytest.raises(DocumentOperationError, match="multiple document references"):
            asyncio.run(service.delete_document(doc.id, owner))
        assert documents.get_document(doc.id, owner).status == DocumentStatus.PENDING
    assert Path(storage.resolve(key)).exists()
    milvus.query_page_chunks.assert_not_called()


def test_legitimate_upload_file_is_still_deleted(repositories, storage_path):
    documents, _, _ = repositories
    storage = LocalFileStorage(str(storage_path))
    key = storage.save("own.txt", b"own content")
    doc = documents.create_document("own.txt", key, "workspace-a")
    milvus = Mock()
    milvus.query_page_chunks.return_value = []
    asyncio.run(DocumentDeleteService(documents, milvus, storage).delete_document(doc.id, "workspace-a"))
    assert not Path(storage.resolve(key)).exists()


class MemoryQueue:
    """Redis substitute; the production Lua deduplication is also checked live."""
    def __init__(self):
        self.pending = []
        self.processing = []
        self.payloads = {}

    def ensure_enqueued(self, job_id, payload, *, ttl_seconds=None):
        self.payloads[job_id] = payload
        if job_id in self.pending or job_id in self.processing:
            return False
        self.pending.append(job_id)
        return True

    def list_processing(self):
        return list(self.processing)

    def recover_processing(self):
        while self.processing:
            self.pending.append(self.processing.pop())

    def dequeue(self, timeout_seconds):
        if not self.pending:
            return None
        job_id = self.pending.pop(0)
        self.processing.append(job_id)
        return job_id

    def get_payload(self, job_id):
        return None  # Exercise execution without any Redis payload.

    def delete_payload(self, job_id):
        self.payloads.pop(job_id, None)

    def acknowledge(self, job_id):
        self.processing.remove(job_id)


def test_api_crash_gap_and_redis_loss_are_repaired_without_duplicate_execution(repositories):
    documents, jobs, plugins = repositories
    queue = MemoryQueue()
    crashing_queue = Mock()
    crashing_queue.ensure_enqueued.side_effect = KeyboardInterrupt("API died after commit")
    settings = Settings(app_master_key="x" * 32)
    with pytest.raises(KeyboardInterrupt):
        IngestJobService(jobs, crashing_queue, settings).create_web_clip_job(
            "workspace-a", WebClipRequest(url="https://example.com/", raw_text="durable source"),
        )
    job = jobs.list_queued_jobs()[0]
    assert job.payload == {"job_type": "WEB_CLIP", "plugin_id": "workspace-a",
                           "url": "https://example.com/", "title": None, "raw_text": "durable source"}
    clip = Mock()

    async def process(**kwargs):
        doc = documents.create_document("webclip.txt", "", "workspace-a", source_type="webpage")
        kwargs["claim_document"](doc.id)
        documents.update_ingest_result(doc.id, chunk_count=1, status="SUCCESS", error_message=None)
        return documents.get_document(doc.id, "workspace-a")

    clip.clip = AsyncMock(side_effect=process)
    plugin_service = Mock()
    plugin_service.decrypt_api_key.return_value = OLD
    worker = IngestWorker(repository=jobs, queue=queue, plugin_repository=plugins,
                          plugin_service=plugin_service, web_clip_service=clip,
                          document_upload_service=Mock(), max_retries=3)
    assert worker.dispatch_queued() == 1
    assert worker.dispatch_queued() == 0
    queue.pending.clear()  # Simulate loss of Redis delivery data after SQL commit.
    queue.payloads.clear()
    assert worker.dispatch_queued() == 1
    assert worker.run_once(timeout_seconds=0)
    finished = jobs.get_job(job.id, "workspace-a")
    assert finished.status == "SUCCEEDED"
    assert finished.payload is None
    assert not worker.run_once(timeout_seconds=0)
    assert clip.clip.await_count == 1


def test_failed_job_retry_uses_sql_payload_after_redis_expiry(repositories):
    _, jobs, _ = repositories
    payload = {"job_type": "WEB_CLIP", "plugin_id": "workspace-a", "url": "https://example.com/", "raw_text": "retry body"}
    job = jobs.create_job("workspace-a", IngestJobType.WEB_CLIP, payload=payload)
    jobs.mark_failed(job.id, "transient model error")
    queue = MemoryQueue()
    result = IngestJobService(jobs, queue, Settings()).retry_job(job.id, "workspace-a")
    assert result.status == "QUEUED"
    assert queue.payloads[job.id] == payload


def test_unrecoverable_legacy_web_jobs_do_not_starve_durable_delivery(repositories):
    _, jobs, _ = repositories
    for _ in range(101):
        jobs.create_job("workspace-a", IngestJobType.WEB_CLIP)
    durable = jobs.create_job("workspace-a", IngestJobType.WEB_CLIP,
                             payload={"job_type": "WEB_CLIP", "plugin_id": "workspace-a"})
    assert [job.id for job in jobs.list_queued_jobs()] == [durable.id]


def test_model_save_rechecks_after_document_created_during_validation(repositories):
    documents, _, plugins = repositories
    embedding = Mock()
    embedding.embed.side_effect = lambda *args, **kwargs: documents.create_document(
        "concurrent.txt", "", "workspace-a", embedding_fingerprint=OLD_FP,
    )
    service = PluginService(plugins, Settings(app_master_key="x" * 32), embedding, Mock(), documents)
    with pytest.raises(ApiKeyValidationError, match="先删除已有文档"):
        service.update_model_config("workspace-a", NEW)
    assert plugins.get_by_plugin_id("workspace-a").embedding_config_fingerprint == OLD_FP


def test_stale_credential_snapshot_cannot_create_or_ingest_after_model_switch(repositories):
    documents, jobs, plugins = repositories
    plugins.update_api_key("workspace-a", "new-ciphertext", "new-nonce", NEW_FP)
    with pytest.raises(DocumentOperationError, match="Embedding configuration changed"):
        documents.create_document("stale.txt", "", "workspace-a", embedding_fingerprint=OLD_FP)
    assert documents.count_documents("workspace-a") == 0
    doc = documents.create_document("new.txt", "", "workspace-a", embedding_fingerprint=NEW_FP)
    ingest = Mock()
    ingest.ingest_page = AsyncMock()
    with pytest.raises(DocumentOperationError, match="Embedding configuration changed"):
        asyncio.run(DocumentIngestService(documents, ingest).ingest_document(
            doc.id, ["text"], "workspace-a", api_key=OLD,
        ))
    ingest.ingest_page.assert_not_awaited()
    job = jobs.create_job("workspace-a", IngestJobType.WEB_CLIP)
    jobs.claim_job(job.id)
    with pytest.raises(DocumentOperationError, match="Embedding configuration changed"):
        jobs.begin_document(job.id, doc.id, embedding_fingerprint=OLD_FP)
    assert documents.get_document(doc.id, "workspace-a").status == "PENDING"


def test_same_embedding_space_key_rotation_remains_allowed(repositories):
    documents, _, plugins = repositories
    documents.create_document("existing.txt", "", "workspace-a")
    result = plugins.update_api_key("workspace-a", "rotated-ciphertext", "new-nonce", OLD_FP)
    assert result.api_key_ciphertext == "rotated-ciphertext"


def test_query_cannot_use_old_model_against_newly_ingested_documents(repositories):
    documents, _, plugins = repositories
    plugins.update_api_key("workspace-a", "new-ciphertext", "new-nonce", NEW_FP)
    doc = documents.create_document("new.txt", "", "workspace-a", embedding_fingerprint=NEW_FP)
    documents.update_ingest_result(doc.id, chunk_count=1, status="SUCCESS", error_message=None)
    embedding = Mock()
    service = RagService(embedding, Mock(), documents)
    with pytest.raises(DocumentOperationError, match="Embedding configuration changed"):
        asyncio.run(service.search("query", plugin_id="workspace-a", api_key=OLD))
    embedding.embed.assert_not_called()


def test_legacy_key_decryption_preserves_model_snapshot_for_guards(repositories):
    _, _, plugins = repositories
    settings = Settings(app_master_key="x" * 32)
    ciphertext, nonce = encrypt_api_key("legacy-synthetic-key", settings.app_master_key)
    workspace = plugins.update_api_key("workspace-a", ciphertext, nonce, OLD_FP)
    credentials = PluginService(plugins, settings).decrypt_api_key(workspace)
    assert isinstance(credentials, WorkspaceModelCredentials)
    assert str(credentials) == "legacy-synthetic-key"
    assert embedding_config_fingerprint(credentials.embedding) == OLD_FP
