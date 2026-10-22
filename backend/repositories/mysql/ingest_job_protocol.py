from __future__ import annotations

from typing import Protocol, runtime_checkable

from ...models.ingest_job import IngestJob


@runtime_checkable
class IngestJobRepository(Protocol):
    def create_job(
        self,
        plugin_id: str,
        job_type: str,
        document_id: int | None = None,
        payload: dict | None = None,
    ) -> IngestJob: ...

    def list_queued_jobs(self, limit: int = 100) -> list[IngestJob]: ...

    def get_job(self, job_id: str, plugin_id: str) -> IngestJob: ...

    def get_document_job(self, document_id: int, plugin_id: str) -> IngestJob: ...

    def claim_job(self, job_id: str) -> IngestJob | None: ...

    def begin_document(self, job_id: str, document_id: int, *, embedding_fingerprint: str | None = None) -> None: ...

    def recover_interrupted(self, job_id: str) -> bool: ...

    def mark_succeeded(self, job_id: str, document_id: int) -> IngestJob: ...

    def mark_failed(self, job_id: str, error_message: str) -> IngestJob: ...

    def requeue(self, job_id: str, plugin_id: str) -> IngestJob: ...
