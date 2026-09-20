from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import Mock

from backend.core.config import Settings
from backend.core.exceptions import IngestJobConflictError, IngestJobOperationError
from backend.models.document_api_schema import WebClipRequest
from backend.models.ingest_job import IngestJobStatus, IngestJobType
from backend.repositories.mysql.ingest_job_protocol import IngestJobRepository
from backend.services.ingest_job import IngestJobService
from backend.tasks.redis_queue import RedisIngestQueue


def make_job(status: str = IngestJobStatus.QUEUED, attempts: int = 0) -> Mock:
    job = Mock()
    job.id = "job-1"
    job.plugin_id = "plugin-a"
    job.job_type = IngestJobType.WEB_CLIP
    job.status = status
    job.attempt_count = attempts
    job.created_at = datetime.now()
    return job


class IngestJobServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = Mock(spec=IngestJobRepository)
        self.queue = Mock(spec=RedisIngestQueue)
        self.settings = Settings(ingest_max_retries=3)
        self.service = IngestJobService(self.repository, self.queue, self.settings)

    def test_create_stores_payload_without_api_key(self) -> None:
        job = make_job()
        self.repository.create_job.return_value = job
        self.repository.get_job.return_value = job
        request = WebClipRequest(
            url="https://example.com/article?utm_source=test",
            title="Article",
            raw_text="body",
        )

        result = self.service.create_web_clip_job("plugin-a", request)

        self.assertIs(result, job)
        payload = self.queue.enqueue.call_args.args[1]
        self.assertNotIn("api_key", payload)
        self.assertEqual(payload["raw_text"], "body")
        self.assertEqual(payload["plugin_id"], "plugin-a")

    def test_enqueue_failure_marks_job_failed(self) -> None:
        job = make_job()
        self.repository.create_job.return_value = job
        self.queue.enqueue.side_effect = IngestJobOperationError("redis down")
        with self.assertRaises(IngestJobOperationError):
            self.service.create_web_clip_job(
                "plugin-a",
                WebClipRequest(url="https://example.com/", raw_text="body"),
            )
        self.repository.mark_failed.assert_called_once_with(job.id, "redis down")

    def test_create_file_upload_stores_only_document_reference(self) -> None:
        job = make_job()
        job.job_type = IngestJobType.FILE_UPLOAD
        job.document_id = 73
        self.repository.create_job.return_value = job
        self.repository.get_job.return_value = job

        result = self.service.create_file_upload_job("plugin-a", 73)

        self.assertIs(result, job)
        self.repository.create_job.assert_called_once_with(
            "plugin-a",
            IngestJobType.FILE_UPLOAD,
            document_id=73,
        )
        payload = self.queue.enqueue.call_args.args[1]
        self.assertEqual(
            payload,
            {
                "job_type": IngestJobType.FILE_UPLOAD,
                "plugin_id": "plugin-a",
                "document_id": 73,
            },
        )

    def test_file_upload_enqueue_failure_marks_job_failed(self) -> None:
        job = make_job()
        job.job_type = IngestJobType.FILE_UPLOAD
        job.document_id = 73
        self.repository.create_job.return_value = job
        self.queue.enqueue.side_effect = IngestJobOperationError("redis down")

        with self.assertRaises(IngestJobOperationError):
            self.service.create_file_upload_job("plugin-a", 73)

        self.repository.mark_failed.assert_called_once_with(job.id, "redis down")

    def test_retry_enforces_state_and_limit(self) -> None:
        running = make_job(IngestJobStatus.RUNNING, attempts=1)
        self.repository.get_job.return_value = running
        with self.assertRaises(IngestJobConflictError):
            self.service.retry_job(running.id, "plugin-a")

        failed = make_job(IngestJobStatus.FAILED, attempts=3)
        self.repository.get_job.return_value = failed
        with self.assertRaises(IngestJobConflictError):
            self.service.retry_job(failed.id, "plugin-a")

    def test_retry_requeues_failed_job(self) -> None:
        failed = make_job(IngestJobStatus.FAILED, attempts=1)
        queued = make_job(IngestJobStatus.QUEUED, attempts=1)
        self.repository.get_job.return_value = failed
        self.repository.requeue.return_value = queued
        result = self.service.retry_job(failed.id, "plugin-a")
        self.assertIs(result, queued)
        self.queue.enqueue_existing.assert_called_once_with(failed.id)

    def test_retry_file_upload_rebuilds_expired_payload(self) -> None:
        failed = make_job(IngestJobStatus.FAILED, attempts=1)
        failed.job_type = IngestJobType.FILE_UPLOAD
        failed.document_id = 73
        queued = make_job(IngestJobStatus.QUEUED, attempts=1)
        queued.job_type = IngestJobType.FILE_UPLOAD
        queued.document_id = 73
        self.repository.get_job.return_value = failed
        self.repository.requeue.return_value = queued

        result = self.service.retry_job(failed.id, "plugin-a")

        self.assertIs(result, queued)
        self.queue.enqueue.assert_called_once_with(
            failed.id,
            {
                "job_type": IngestJobType.FILE_UPLOAD,
                "plugin_id": "plugin-a",
                "document_id": 73,
            },
        )
        self.queue.enqueue_existing.assert_not_called()
