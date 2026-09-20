from __future__ import annotations

from ..core.config import Settings
from ..core.exceptions import IngestJobConflictError, IngestJobOperationError
from ..models.document_api_schema import WebClipRequest
from ..models.ingest_job import IngestJob, IngestJobStatus, IngestJobType
from ..repositories.mysql.ingest_job_protocol import IngestJobRepository
from ..tasks.redis_queue import RedisIngestQueue


class IngestJobService:
    def __init__(
        self,
        repository: IngestJobRepository,
        queue: RedisIngestQueue,
        settings: Settings,
    ) -> None:
        self._repository = repository
        self._queue = queue
        self._max_retries = settings.ingest_max_retries

    def create_web_clip_job(
        self,
        plugin_id: str,
        request: WebClipRequest,
    ) -> IngestJob:
        job = self._repository.create_job(plugin_id, IngestJobType.WEB_CLIP)
        payload = {
            "job_type": IngestJobType.WEB_CLIP,
            "plugin_id": plugin_id,
            "url": request.url,
            "title": request.title,
            "raw_text": request.raw_text,
        }
        try:
            self._queue.enqueue(job.id, payload)
        except IngestJobOperationError as exc:
            self._repository.mark_failed(job.id, str(exc))
            raise
        return self._repository.get_job(job.id, plugin_id)

    def create_file_upload_job(
        self,
        plugin_id: str,
        document_id: int,
    ) -> IngestJob:
        job = self._repository.create_job(
            plugin_id,
            IngestJobType.FILE_UPLOAD,
            document_id=document_id,
        )
        payload = {
            "job_type": IngestJobType.FILE_UPLOAD,
            "plugin_id": plugin_id,
            "document_id": document_id,
        }
        try:
            self._queue.enqueue(job.id, payload)
        except IngestJobOperationError as exc:
            self._repository.mark_failed(job.id, str(exc))
            raise
        return self._repository.get_job(job.id, plugin_id)

    def get_job(self, job_id: str, plugin_id: str) -> IngestJob:
        return self._repository.get_job(job_id, plugin_id)

    def retry_job(self, job_id: str, plugin_id: str) -> IngestJob:
        job = self._repository.get_job(job_id, plugin_id)
        if job.status != IngestJobStatus.FAILED:
            raise IngestJobConflictError("only FAILED jobs can be retried")
        if job.attempt_count >= self._max_retries:
            raise IngestJobConflictError("ingest job retry limit reached")
        if job.job_type == IngestJobType.FILE_UPLOAD and job.document_id is None:
            raise IngestJobConflictError("uploaded document no longer exists")
        queued = self._repository.requeue(job_id, plugin_id)
        try:
            if queued.job_type == IngestJobType.FILE_UPLOAD:
                self._queue.enqueue(
                    job_id,
                    {
                        "job_type": IngestJobType.FILE_UPLOAD,
                        "plugin_id": plugin_id,
                        "document_id": queued.document_id,
                    },
                )
            else:
                self._queue.enqueue_existing(job_id)
        except IngestJobOperationError as exc:
            self._repository.mark_failed(job_id, str(exc))
            raise
        return queued
