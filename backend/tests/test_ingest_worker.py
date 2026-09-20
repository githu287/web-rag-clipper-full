from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock

from backend.models.ingest_job import IngestJobType
from backend.models.plugin import PluginStatus
from backend.repositories.mysql.ingest_job_protocol import IngestJobRepository
from backend.repositories.mysql.plugin_protocol import PluginRepository
from backend.services.plugin_service import PluginService
from backend.services.document_upload import DocumentUploadService
from backend.services.web_clip import WebClipService
from backend.tasks.redis_queue import RedisIngestQueue
from backend.workers.ingest_worker import IngestWorker


class IngestWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = Mock(spec=IngestJobRepository)
        self.queue = Mock(spec=RedisIngestQueue)
        self.plugin_repository = Mock(spec=PluginRepository)
        self.plugin_service = Mock(spec=PluginService)
        self.web_clip_service = Mock(spec=WebClipService)
        self.web_clip_service.clip = AsyncMock()
        self.document_upload_service = Mock(spec=DocumentUploadService)
        self.document_upload_service.process_upload = AsyncMock()
        self.worker = IngestWorker(
            repository=self.repository,
            queue=self.queue,
            plugin_repository=self.plugin_repository,
            plugin_service=self.plugin_service,
            web_clip_service=self.web_clip_service,
            document_upload_service=self.document_upload_service,
        )

    def test_successful_job_reaches_terminal_state_and_acknowledges(self) -> None:
        job = Mock(
            id="job-1",
            plugin_id="plugin-a",
            job_type=IngestJobType.WEB_CLIP,
            document_id=None,
        )
        plugin = Mock(status=PluginStatus.ACTIVE)
        document = Mock(id=42)
        self.queue.dequeue.return_value = "job-1"
        self.repository.claim_job.return_value = job
        self.queue.get_payload.return_value = {
            "job_type": IngestJobType.WEB_CLIP,
            "plugin_id": "plugin-a",
            "url": "https://example.com/",
            "title": "Example",
            "raw_text": "body",
        }
        self.plugin_repository.get_by_plugin_id.return_value = plugin
        self.plugin_service.decrypt_api_key.return_value = "sk-test"
        self.web_clip_service.clip.return_value = document

        self.assertTrue(self.worker.run_once(timeout_seconds=0))

        self.repository.mark_succeeded.assert_called_once_with("job-1", 42)
        self.queue.delete_payload.assert_called_once_with("job-1")
        self.queue.acknowledge.assert_called_once_with("job-1")
        self.repository.mark_failed.assert_not_called()

    def test_file_upload_job_processes_prepared_document(self) -> None:
        job = Mock(
            id="job-2",
            plugin_id="plugin-a",
            job_type=IngestJobType.FILE_UPLOAD,
            document_id=73,
        )
        plugin = Mock(status=PluginStatus.ACTIVE)
        document = Mock(id=73)
        self.queue.dequeue.return_value = "job-2"
        self.repository.claim_job.return_value = job
        self.queue.get_payload.return_value = {
            "job_type": IngestJobType.FILE_UPLOAD,
            "plugin_id": "plugin-a",
            "document_id": 73,
        }
        self.plugin_repository.get_by_plugin_id.return_value = plugin
        self.plugin_service.decrypt_api_key.return_value = "sk-test"
        self.document_upload_service.process_upload.return_value = document

        self.assertTrue(self.worker.run_once(timeout_seconds=0))

        self.document_upload_service.process_upload.assert_awaited_once_with(
            document_id=73,
            plugin_id="plugin-a",
            api_key="sk-test",
        )
        self.repository.mark_succeeded.assert_called_once_with("job-2", 73)
        self.queue.delete_payload.assert_called_once_with("job-2")
        self.queue.acknowledge.assert_called_once_with("job-2")
        self.web_clip_service.clip.assert_not_awaited()

    def test_file_upload_failure_marks_job_and_document_failed(self) -> None:
        job = Mock(
            id="job-2",
            plugin_id="plugin-a",
            job_type=IngestJobType.FILE_UPLOAD,
            document_id=73,
        )
        self.queue.dequeue.return_value = "job-2"
        self.repository.claim_job.return_value = job
        self.queue.get_payload.return_value = {
            "job_type": IngestJobType.FILE_UPLOAD,
            "plugin_id": "plugin-a",
            "document_id": 73,
        }
        self.plugin_repository.get_by_plugin_id.return_value = Mock(
            status=PluginStatus.ACTIVE
        )
        self.plugin_service.decrypt_api_key.return_value = "sk-test"
        self.document_upload_service.process_upload.side_effect = RuntimeError(
            "parse failed"
        )

        self.assertTrue(self.worker.run_once(timeout_seconds=0))

        self.document_upload_service.fail_prepared_upload.assert_called_once()
        self.repository.mark_failed.assert_called_once_with("job-2", "parse failed")
        self.queue.acknowledge.assert_called_once_with("job-2")

    def test_missing_payload_is_failed_and_acknowledged(self) -> None:
        job = Mock(id="job-1", plugin_id="plugin-a")
        self.queue.dequeue.return_value = "job-1"
        self.repository.claim_job.return_value = job
        self.queue.get_payload.return_value = None

        self.assertTrue(self.worker.run_once(timeout_seconds=0))

        self.repository.mark_failed.assert_called_once()
        self.queue.acknowledge.assert_called_once_with("job-1")

    def test_duplicate_delivery_is_only_acknowledged(self) -> None:
        self.queue.dequeue.return_value = "job-1"
        self.repository.claim_job.return_value = None
        self.assertTrue(self.worker.run_once(timeout_seconds=0))
        self.queue.acknowledge.assert_called_once_with("job-1")
        self.queue.get_payload.assert_not_called()

    def test_recovery_resets_processing_jobs(self) -> None:
        self.queue.recover_processing.return_value = ["job-1", "job-2"]
        self.assertEqual(self.worker.recover_interrupted(), 2)
        self.assertEqual(self.repository.recover_interrupted.call_count, 2)
