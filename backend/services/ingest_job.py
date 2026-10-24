from __future__ import annotations

import logging

from ..core.config import Settings
from ..core.exceptions import IngestJobConflictError, IngestJobNotFoundError, IngestJobOperationError
from ..models.document_api_schema import WebClipRequest
from ..models.ingest_job import IngestJob, IngestJobStatus, IngestJobType
from ..repositories.mysql.ingest_job_protocol import IngestJobRepository
from ..tasks.redis_queue import RedisIngestQueue
from ..tasks.payload import durable_payload, payload_ttl

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
        self._payload_ttl = settings.ingest_payload_ttl_seconds

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
        job = self._repository.create_job(
            plugin_id, IngestJobType.WEB_CLIP,
            payload=payload, payload_ttl_seconds=self._payload_ttl,
        )
        try:
            self._queue.enqueue(job.id, payload, ttl_seconds=payload_ttl(job))
        except (IngestJobOperationError, ValueError):
            logger.warning("durable ingest publication deferred: job_id=%s", job.id)
        # The DB commit is durable acceptance, even when Redis is unavailable.
        # Polling provides current progress without a fallible readback here.
        return job

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
        except IngestJobOperationError:
            logger.warning("upload publication deferred: job_id=%s", job.id)
        return job

    def get_job(self, job_id: str, plugin_id: str) -> IngestJob:
        return self._repository.get_job(job_id, plugin_id, include_payload=False)

    def retry_job(self, job_id: str, plugin_id: str) -> IngestJob:
        job = self._repository.get_job(job_id, plugin_id)
        if job.status != IngestJobStatus.FAILED:
            raise IngestJobConflictError("only FAILED jobs can be retried")
        if job.attempt_count >= self._max_retries:
            raise IngestJobConflictError("ingest job retry limit reached")
        if job.job_type == IngestJobType.FILE_UPLOAD and job.document_id is None:
            raise IngestJobConflictError("uploaded document no longer exists")
        try:
            durable_payload(job)
        except ValueError as exc:
            raise IngestJobConflictError(str(exc)) from exc
        queued = self._repository.requeue(job_id, plugin_id, max_attempts=self._max_retries)
        try:
            payload = durable_payload(queued)
        except ValueError as exc:
            raise IngestJobConflictError(str(exc)) from exc
        try:
            if queued.job_type == IngestJobType.FILE_UPLOAD:
                self._queue.enqueue(job_id, payload)
            elif payload is not None:
                self._queue.enqueue(job_id, payload, ttl_seconds=payload_ttl(queued))
            else:
                self._queue.enqueue_existing(job_id)
        except IngestJobOperationError as exc:
            if payload is None:
                # Old web jobs have no durable body. Keep their original
                # fail-and-retry behavior when Redis cannot publish them.
                self._fail_submission(queued, exc)
                raise
            logger.warning("durable ingest retry publication deferred: job_id=%s", job_id)
        except ValueError as exc:
            raise IngestJobConflictError(str(exc)) from exc
        return queued

    def _fail_submission(self, job: IngestJob, exc: IngestJobOperationError) -> None:
        try:
            self._repository.mark_failed(
                job.id, str(exc), expected_attempt_count=job.attempt_count,
            )
        except (IngestJobConflictError, IngestJobNotFoundError):
            # Redis may have accepted the command before losing its reply.
            # Preserve a worker's result and retain the original queue error.
            logger.info("submission compensation skipped: job_id=%s", job.id)
        except IngestJobOperationError:
            logger.exception("submission compensation unavailable: job_id=%s", job.id)
