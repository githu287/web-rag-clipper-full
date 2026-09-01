"""Validate aligned Evaluation IDs against live MySQL and Milvus state."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select

from backend.core.config import get_settings
from backend.core.db import get_engine, get_session_factory
from backend.models.document import Document, DocumentStatus
from backend.models.plugin import PluginStatus, PluginWorkspace
from backend.repositories.milvus.impl import PyMilvusRepositoryImpl

from .baseline import load_jsonl, validate_rows


@dataclass(frozen=True)
class RuntimeDocument:
    id: int
    plugin_id: str
    status: str
    chunk_count: int


def _document_ids(row: dict[str, Any]) -> set[int]:
    values: list[Any] = []
    for field in ("document_id",):
        if row.get(field) is not None:
            values.append(row[field])
    truth = row.get("retrieval_ground_truth") or {}
    for field in ("gold_document_ids", "forbidden_document_ids"):
        values.extend(truth.get(field) or [])
    values.extend(row.get("interference_doc_ids") or [])
    return {int(value) for value in values}


def validate_runtime_rows(
    rows: Iterable[dict[str, Any]],
    *,
    documents: dict[int, RuntimeDocument],
    chunks_by_document: dict[int, set[str]],
    plugin_statuses: dict[str, str],
    source: str,
) -> list[str]:
    """Validate ownership, status and exact MySQL↔Milvus chunk identity."""
    errors: list[str] = []
    for index, row in enumerate(rows, 1):
        prefix = f"{source}:{index}({row.get('id', '?')})"
        active_plugin = str(row.get("target_plugin_id") or row.get("plugin_id") or "")
        if not active_plugin:
            errors.append(f"{prefix}: missing active plugin id")
            continue
        if plugin_statuses.get(active_plugin) != PluginStatus.ACTIVE:
            errors.append(f"{prefix}: active plugin is missing or not ACTIVE")

        for document_id in _document_ids(row):
            document = documents.get(document_id)
            if document is None:
                errors.append(f"{prefix}: document {document_id} does not exist")
            elif document.status != DocumentStatus.SUCCESS:
                errors.append(
                    f"{prefix}: document {document_id} is {document.status}, not SUCCESS"
                )

        truth = row.get("retrieval_ground_truth") or {}
        gold_documents = {int(value) for value in truth.get("gold_document_ids") or []}
        forbidden_documents = {
            int(value) for value in truth.get("forbidden_document_ids") or []
        }
        for document_id in gold_documents:
            document = documents.get(document_id)
            if document and document.plugin_id != active_plugin:
                errors.append(
                    f"{prefix}: gold document {document_id} belongs to another plugin"
                )
        for document_id in forbidden_documents:
            document = documents.get(document_id)
            if document and document.plugin_id == active_plugin:
                errors.append(
                    f"{prefix}: forbidden document {document_id} belongs to active plugin"
                )

        current_id = row.get("document_id")
        if current_id is not None:
            current = documents.get(int(current_id))
            is_cross_workspace_probe = (
                row.get("test_layer") == "L5_CrossWorkspace_Ownership"
            )
            if current:
                owner_matches = current.plugin_id == active_plugin
                if is_cross_workspace_probe and owner_matches:
                    errors.append(
                        f"{prefix}: L5 probe document unexpectedly belongs to active plugin"
                    )
                if not is_cross_workspace_probe and not owner_matches:
                    errors.append(
                        f"{prefix}: current document belongs to another plugin"
                    )

        for chunk_id in truth.get("gold_chunk_ids") or []:
            chunk_text = str(chunk_id)
            page_text, separator, index_text = chunk_text.rpartition("_")
            if not separator or not page_text.isdigit() or not index_text.isdigit():
                errors.append(f"{prefix}: invalid gold chunk id {chunk_text!r}")
                continue
            page_id = int(page_text)
            if page_id not in gold_documents:
                errors.append(
                    f"{prefix}: gold chunk {chunk_text} is not from a gold document"
                )
            if chunk_text not in chunks_by_document.get(page_id, set()):
                errors.append(
                    f"{prefix}: gold chunk {chunk_text} does not exist in Milvus"
                )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path, nargs="+")
    args = parser.parse_args()

    loaded: list[tuple[Path, list[dict[str, Any]]]] = []
    document_ids: set[int] = set()
    plugin_ids: set[str] = set()
    errors: list[str] = []
    for path in args.dataset:
        rows = load_jsonl(path)
        errors.extend(validate_rows(rows, source=str(path)))
        loaded.append((path, rows))
        for row in rows:
            document_ids.update(_document_ids(row))
            for field in (
                "plugin_id",
                "source_plugin_id",
                "target_plugin_id",
                "probe_plugin_id",
            ):
                if row.get(field):
                    plugin_ids.add(str(row[field]))
    if errors:
        print("\n".join(errors))
        return 1

    factory = get_session_factory(get_engine())
    with factory() as session:
        document_models = session.scalars(
            select(Document).where(Document.id.in_(document_ids))
        ).all()
        plugin_models = session.scalars(
            select(PluginWorkspace).where(
                PluginWorkspace.plugin_id.in_(plugin_ids)
            )
        ).all()
    documents = {
        item.id: RuntimeDocument(
            item.id, item.plugin_id, item.status, item.chunk_count
        )
        for item in document_models
    }
    plugin_statuses = {item.plugin_id: item.status for item in plugin_models}

    milvus = PyMilvusRepositoryImpl(get_settings())
    chunks_by_document = {
        document_id: set(milvus.query_page_chunks(document_id))
        for document_id in document_ids
    }
    for document_id, document in documents.items():
        actual_count = len(chunks_by_document.get(document_id, set()))
        if actual_count != document.chunk_count:
            errors.append(
                f"document {document_id}: MySQL chunk_count={document.chunk_count}, "
                f"Milvus count={actual_count}"
            )

    for path, rows in loaded:
        errors.extend(
            validate_runtime_rows(
                rows,
                documents=documents,
                chunks_by_document=chunks_by_document,
                plugin_statuses=plugin_statuses,
                source=str(path),
            )
        )
    if errors:
        print("\n".join(errors))
        return 1
    print(
        f"runtime alignment valid: {sum(len(rows) for _, rows in loaded)} cases, "
        f"{len(documents)} documents, "
        f"{sum(len(value) for value in chunks_by_document.values())} chunks"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
