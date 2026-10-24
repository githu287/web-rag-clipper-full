"""Deletion must keep the API responsive and retain recoverable resources."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from backend.core.exceptions import PluginOperationError
from backend.models.base import Base
from backend.models.document import DocumentStatus
from backend.repositories.mysql import DocumentRepositoryImpl, PluginRepositoryImpl
from backend.services.document_delete import DocumentDeleteService
from backend.services.plugin_service import PluginService
from backend.services.workspace_delete import WorkspaceDeleteService


def assert_responsive(operation, slow_dependency, result):
    """Only the event loop can release the simulated slow storage operation."""
    started, release = threading.Event(), threading.Event()

    def slow(*args, **kwargs):
        started.set()
        if not release.wait(2):
            raise AssertionError("deletion blocked the event loop")
        return result

    slow_dependency.side_effect = slow

    async def scenario():
        task = asyncio.create_task(operation())
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()
        finally:
            release.set()
        await task

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", [
    "get_document", "transition_status", "query_page_chunks",
    "delete_chunks", "delete_file", "delete_document",
])
def test_document_deletion_keeps_event_loop_responsive(boundary):
    document = SimpleNamespace(status=DocumentStatus.SUCCESS,
                               source_type="upload", file_path="test.txt")
    documents = Mock(get_document=Mock(return_value=document))
    milvus = Mock(query_page_chunks=Mock(return_value=["1_0"]))
    files = Mock()
    service = DocumentDeleteService(documents, milvus, files)
    dependencies = {
        "get_document": (documents.get_document, document),
        "transition_status": (documents.transition_status, None),
        "query_page_chunks": (milvus.query_page_chunks, ["1_0"]),
        "delete_chunks": (milvus.delete_chunks, None),
        "delete_file": (files.delete, None),
        "delete_document": (documents.delete_document, None),
    }
    assert_responsive(lambda: service.delete_document(1, "a"),
                      *dependencies[boundary])
    documents.delete_document.assert_called_once_with(1, "a")


@pytest.mark.parametrize("boundary", ["get_plugin", "list_documents", "delete_workspace"])
def test_workspace_deletion_keeps_event_loop_responsive(boundary):
    workspace = SimpleNamespace(plugin_name="Test")
    plugins = Mock(get_plugin=Mock(return_value=workspace))
    documents = Mock(list_documents=Mock(return_value=[]))
    service = WorkspaceDeleteService(plugins, documents,
                                     Mock(delete_document=AsyncMock()))
    dependencies = {
        "get_plugin": (plugins.get_plugin, workspace),
        "list_documents": (documents.list_documents, []),
        "delete_workspace": (plugins.delete_workspace, None),
    }
    assert_responsive(lambda: service.delete_workspace("a", confirm=True, plugin_name="Test"),
                      *dependencies[boundary])
    plugins.delete_workspace.assert_called_once_with("a", True, "Test")


@pytest.fixture
def repositories():
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    plugins, documents = PluginRepositoryImpl(engine), DocumentRepositoryImpl(engine)
    for name in ("a", "b"):
        plugins.create_plugin(name, name, name, name * 64)
    yield plugins, documents
    engine.dispose()


def test_workspace_repository_refuses_remaining_documents(repositories):
    plugins, documents = repositories
    document = documents.create_document("test.txt", "test.txt", "a")
    with pytest.raises(PluginOperationError, match="still contains documents"):
        plugins.delete_plugin("a")
    assert plugins.get_by_plugin_id("a") is not None
    assert documents.get_document(document.id, "a").status == DocumentStatus.PENDING


def test_other_workspace_documents_do_not_block_deletion(repositories):
    plugins, documents = repositories
    document = documents.create_document("test.txt", "test.txt", "b")
    plugins.delete_plugin("a")
    assert plugins.get_by_plugin_id("a") is None
    assert documents.get_document(document.id, "b") is not None


def test_workspace_late_document_is_retained_and_retry_cleans_resources(repositories):
    plugins, documents = repositories
    plugin_service = PluginService(plugins, SimpleNamespace(app_master_key="test"))
    milvus = Mock(query_page_chunks=Mock(return_value=["1_0"]))
    files = Mock()
    document_delete = DocumentDeleteService(documents, milvus, files)
    listing = Mock()

    def insert_after_empty_listing(*args, **kwargs):
        # Another request commits after the last page was read as empty.
        documents.create_document("test.txt", "test.txt", "a")
        return []

    listing.list_documents.side_effect = insert_after_empty_listing
    service = WorkspaceDeleteService(plugin_service, listing, document_delete)
    with pytest.raises(PluginOperationError, match="still contains documents"):
        asyncio.run(service.delete_workspace("a", confirm=True, plugin_name="a"))
    assert plugins.get_by_plugin_id("a") is not None
    assert documents.count_documents("a") == 1
    milvus.delete_chunks.assert_not_called()
    files.delete.assert_not_called()

    listing.list_documents.side_effect = documents.list_documents
    asyncio.run(service.delete_workspace("a", confirm=True, plugin_name="a"))
    milvus.delete_chunks.assert_called_once_with(["1_0"])
    files.delete.assert_called_once_with("test.txt")
    assert documents.count_documents("a") == 0
    assert plugins.get_by_plugin_id("a") is None
