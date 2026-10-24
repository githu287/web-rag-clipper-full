from __future__ import annotations

from typing import Protocol, runtime_checkable
from typing import Any

from ...models.ingest_job import IngestJob


@runtime_checkable
class IngestJobRepository(Protocol):
    def create_job(
        self,
        plugin_id: str,
        job_type: str,
        document_id: int | None = None,
        *,
        payload: dict[str, Any] | None = None,
        payload_ttl_seconds: int | None = None,
    ) -> IngestJob: ...

    def get_job(self, job_id: str, plugin_id: str, *, include_payload: bool = True) -> IngestJob: ...

    def list_queued(self, limit: int = 20) -> list[IngestJob]: ...

    def list_running_ids(self, limit: int = 100) -> list[str]: ...

    def purge_expired_payloads(self, limit: int = 20) -> int: ...

    def claim_job(self, job_id: str) -> IngestJob | None: ...

    def begin_document(self, job_id: str, document_id: int) -> None: ...

    def recover_interrupted(self, job_id: str, max_attempts: int | None = None) -> bool: ...

    def mark_succeeded(self, job_id: str, document_id: int) -> IngestJob: ...

    def mark_failed(
        self, job_id: str, error_message: str, *,
        expected_attempt_count: int | None = None,
    ) -> IngestJob:
        """If specified, require QUEUED and matching attempts before any write."""
        ...

    def requeue(self, job_id: str, plugin_id: str, *, max_attempts: int | None = None) -> IngestJob: ...
