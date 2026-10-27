"""Immediate deletion must observe vectors just written by another process."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from backend.repositories.milvus.impl import PyMilvusRepositoryImpl
from backend.services.document_delete import DocumentDeleteService


def test_immediate_delete_sees_recent_worker_vectors():
    repository = PyMilvusRepositoryImpl(SimpleNamespace(
        milvus_host="localhost", milvus_port=19530,
        milvus_collection="page_chunks", bailian_embedding_dimension=4,
    ))
    # Model cross-process bounded visibility: only a strong read observes the
    # worker's new chunk before deleting the SQL document.
    client = Mock()
    client.query.side_effect = lambda **options: (
        [{"id": "7_0"}] if options.get("consistency_level") == "Strong" else []
    )
    repository._client = client
    documents = Mock()
    documents.get_document.return_value = SimpleNamespace(
        id=7, plugin_id="owner", status="SUCCESS", source_type="webpage", file_path="",
    )
    service = DocumentDeleteService(documents, repository, Mock())
    asyncio.run(service.delete_document(7, "owner"))
    client.delete.assert_called_once_with(collection_name="page_chunks", ids=["7_0"])
    documents.delete_document.assert_called_once_with(7, "owner")
