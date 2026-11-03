"""Read-only checks of historical citations; no credentials or model calls."""
from ..models.api_schema import RagSourceReference, RagSourceStatus
from ..models.document import DocumentStatus
from ..repositories.mysql import DocumentRepository


def check_sources(
    sources: list[RagSourceReference], plugin_id: str, repository: DocumentRepository,
) -> list[RagSourceStatus]:
    ids = list(dict.fromkeys(source.document_id for source in sources))
    documents = {doc.id: doc for doc in repository.get_documents_by_ids(ids, plugin_id)}
    results = []
    for source in sources:
        doc = documents.get(source.document_id)
        if doc is None:
            state = "missing"  # Missing and foreign-owned documents are indistinguishable.
        elif doc.status != DocumentStatus.SUCCESS:
            state = "unavailable"
        elif source.content_revision is None:
            state = "unversioned"
        elif source.content_revision != doc.content_revision:
            state = "changed"
        else:
            state = "current"
        results.append(RagSourceStatus(**source.model_dump(), state=state))
    return results
