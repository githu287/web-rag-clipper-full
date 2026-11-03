"""Historical citations: scoped SQL snapshots, no model configuration required."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from backend.api.deps import get_current_plugin
from backend.core.di import get_document_repository, get_plugin_service
from backend.main import create_app
from backend.models.base import Base
from backend.models.document import DocumentStatus as DS
from backend.repositories.mysql import DocumentRepositoryImpl, PluginRepositoryImpl


@pytest.fixture
def fixture(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    plugins = PluginRepositoryImpl(engine)
    for owner in ("a", "b"):
        plugins.create_plugin(owner, owner, owner, owner * 64)
    documents = DocumentRepositoryImpl(engine)
    doc = documents.create_document("note.md", "", "a")
    doc = documents.update_ingest_result(doc.id, chunk_count=1, status=DS.SUCCESS)
    app = create_app()
    app.dependency_overrides[get_current_plugin] = lambda: SimpleNamespace(plugin_id="a")
    app.dependency_overrides[get_document_repository] = lambda: documents
    plugin_service = Mock()
    plugin_service.decrypt_api_key.side_effect = AssertionError("source checks must not need model credentials")
    app.dependency_overrides[get_plugin_service] = lambda: plugin_service
    monkeypatch.setattr("backend.main.get_milvus_initializer", lambda: Mock())
    with TestClient(app) as client:
        yield client, app, engine, documents, doc, plugin_service
    engine.dispose()


def ref(doc, revision=None):
    return {"document_id": doc.id, "content_revision": revision}


def test_batch_statuses_owner_filter_and_multiple_versions_use_one_read(fixture):
    client, _, engine, documents, doc, plugin_service = fixture
    foreign = documents.create_document("foreign.md", "private/file.md", "b")
    documents.update_ingest_result(foreign.id, chunk_count=1, status=DS.SUCCESS)
    pending = documents.create_document("pending.md", "", "a")
    refs = [ref(doc, doc.content_revision), ref(doc, 0), ref(doc), ref(foreign, 1),
            ref(pending, 0), {"document_id": 999, "content_revision": 0}, ref(doc, doc.content_revision)]
    reads = []
    def record(*args):
        reads.append(args[2])
    event.listen(engine, "before_cursor_execute", record)
    try:
        response = client.post("/rag/sources/check", json={"sources": refs})
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200
    rows = response.json()["sources"]
    assert [r["state"] for r in rows] == ["current", "changed", "unversioned", "missing", "unavailable", "missing", "current"]
    assert [{k: r[k] for k in ("document_id", "content_revision")} for r in rows] == refs
    assert all(set(r) == {"document_id", "content_revision", "state"} for r in rows)
    assert len(reads) == 1
    assert documents.get_document(doc.id, "a").content_revision == doc.content_revision
    plugin_service.decrypt_api_key.assert_not_called()


@pytest.mark.parametrize("status", [DS.PROCESSING, DS.PENDING, DS.FAILED, DS.DELETING])
def test_unavailable_document_is_not_reported_as_current(fixture, status):
    client, _, _, documents, doc, _ = fixture
    documents.update_status(doc.id, status)
    response = client.post("/rag/sources/check", json={"sources": [ref(doc, doc.content_revision)]})
    assert response.json()["sources"][0]["state"] == "unavailable"


def test_completed_reclip_and_delete_are_visible_to_old_citation(fixture):
    client, _, _, documents, doc, _ = fixture
    source = ref(doc, doc.content_revision)
    documents.transition_status(doc.id, "a", {DS.SUCCESS}, DS.PROCESSING)
    updated = documents.update_ingest_result(doc.id, chunk_count=1, status=DS.SUCCESS)
    response = client.post("/rag/sources/check", json={"sources": [source, ref(doc, updated.content_revision)]})
    assert [r["state"] for r in response.json()["sources"]] == ["changed", "current"]
    documents.delete_document(doc.id, "a")
    assert client.post("/rag/sources/check", json={"sources": [source]}).json()["sources"][0]["state"] == "missing"


@pytest.mark.parametrize("payload", [
    {}, {"sources": []}, {"sources": [{"document_id": 1}] * 101},
    {"sources": [{"document_id": True}]}, {"sources": [{"document_id": 0}]},
    {"sources": [{"document_id": "1"}]}, {"sources": [{"document_id": 1, "content_revision": -1}]},
    {"sources": [{"document_id": 1, "content_revision": True}]},
    {"sources": [{"document_id": 1, "content_revision": 1.5}]},
    {"sources": [{"document_id": 1, "plugin_id": "b"}]}, {"sources": [{"document_id": 1}], "plugin_id": "b"},
])
def test_invalid_or_oversized_checks_are_rejected(fixture, payload):
    client, _, _, _, _, _ = fixture
    assert client.post("/rag/sources/check", json=payload).status_code == 422


def test_max_batch_and_legacy_missing_version(fixture):
    client, _, _, _, doc, _ = fixture
    response = client.post("/rag/sources/check", json={"sources": [{"document_id": doc.id}] * 100})
    assert response.status_code == 200
    assert len(response.json()["sources"]) == 100
    assert all(r["state"] == "unversioned" for r in response.json()["sources"])


def test_source_checks_require_workspace_authentication(fixture):
    client, app, _, _, doc, _ = fixture
    app.dependency_overrides.pop(get_current_plugin)
    assert client.post("/rag/sources/check", json={"sources": [ref(doc)]}).status_code == 401


def test_repository_failure_is_not_reported_as_deleted_or_current(fixture, monkeypatch):
    from backend.core.exceptions import DocumentOperationError
    client, _, _, documents, doc, _ = fixture
    def fail(*args):
        raise DocumentOperationError("fixture storage failure")
    monkeypatch.setattr(documents, "get_documents_by_ids", fail)
    response = client.post("/rag/sources/check", json={"sources": [ref(doc)]})
    assert response.status_code == 503
    assert "sources" not in response.json()


def test_blocking_repository_read_is_offloaded_from_event_loop(fixture, monkeypatch):
    client, app, _, documents, doc, _ = fixture
    threads = []
    original = documents.get_documents_by_ids
    async def identity():
        threads.append(threading.get_ident())
        return SimpleNamespace(plugin_id="a")
    def read(*args):
        assert threading.get_ident() != threads[0]
        return original(*args)
    app.dependency_overrides[get_current_plugin] = identity
    monkeypatch.setattr(documents, "get_documents_by_ids", read)
    assert client.post("/rag/sources/check", json={"sources": [ref(doc)]}).status_code == 200
