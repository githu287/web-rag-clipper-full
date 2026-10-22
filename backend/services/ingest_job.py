from __future__ import annotations

import logging

from ..core.config import Settings
from ..core.exceptions import IngestJobConflictError, IngestJobOperationError
from ..models.document_api_schema import WebClipRequest
from ..models.ingest_job import IngestJob, IngestJobStatus, IngestJobType
from ..repositories.mysql.ingest_job_protocol import IngestJobRepository
from ..tasks.redis_queue import RedisIngestQueue

logger = logging.getLogger(__name__)


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
        payload = {
            "job_type": IngestJobType.WEB_CLIP,
            "plugin_id": plugin_id,
            "url": request.url,
            "title": request.title,
            "raw_text": request.raw_text,
        }
        job = self._repository.create_job(plugin_id, IngestJobType.WEB_CLIP, payload=payload)
        self._deliver(job.id, payload)
        return self._repository.get_job(job.id, plugin_id)

    def create_file_upload_job(
        self,
        plugin_id: str,
        document_id: int,
    ) -> IngestJob:
        payload = {
            "job_type": IngestJobType.FILE_UPLOAD,
            "plugin_id": plugin_id,
            "document_id": document_id,
        }
        job = self._repository.create_job(
            plugin_id, IngestJobType.FILE_UPLOAD,
            document_id=document_id, payload=payload,
        )
        self._deliver(job.id, payload)
        return self._repository.get_job(job.id, plugin_id)

    def _deliver(self, job_id: str, payload: dict) -> None:
        try:
            self._queue.ensure_enqueued(job_id, payload)
        except IngestJobOperationError:
            # The committed job is accepted even while Redis is unavailable.
            # Worker reconciliation will restore delivery from MySQL.
            logger.warning("ingest delivery deferred: job_id=%s", job_id)

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
        if isinstance(getattr(queued, "payload", None), dict):
            self._deliver(job_id, queued.payload)
            return queued
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
