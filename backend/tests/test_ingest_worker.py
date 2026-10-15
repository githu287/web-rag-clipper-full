from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from backend.core.exceptions import IngestJobConflictError, IngestJobOperationError
from backend.models.ingest_job import IngestJobStatus, IngestJobType
from backend.models.plugin import PluginStatus
from backend.repositories.mysql.ingest_job_protocol import IngestJobRepository
from backend.repositories.mysql.plugin_protocol import PluginRepository
from backend.services.plugin_service import PluginService
from backend.services.document_upload import DocumentUploadService
from backend.services.web_clip import WebClipService
from backend.tasks.redis_queue import RedisIngestQueue
from backend.workers import ingest_worker
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
            max_retries=3,
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
        self.queue.list_processing.return_value = ["job-1", "job-2"]
        self.assertEqual(self.worker.recover_interrupted(), 2)
        self.assertEqual(self.repository.recover_interrupted.call_count, 2)
        self.queue.recover_processing.assert_called_once_with()

    def test_recovery_keeps_processing_until_database_resets_finish(self) -> None:
        """回归：数据库复位半途失败时不得搬运队列，否则任务会被 claim 跳过并静默丢弃。"""
        self.queue.list_processing.return_value = ["job-1", "job-2"]
        self.repository.recover_interrupted.side_effect = IngestJobOperationError(
            "requeue ingest job failed"
        )

        with self.assertRaises(IngestJobOperationError):
            self.worker.recover_interrupted()

        self.queue.recover_processing.assert_not_called()

    def test_recovery_gives_up_when_retry_limit_reached(self) -> None:
        job = Mock(
            id="job-9",
            plugin_id="plugin-a",
            status=IngestJobStatus.RUNNING,
            attempt_count=3,
            job_type=IngestJobType.FILE_UPLOAD,
            document_id=73,
        )
        self.queue.list_processing.return_value = ["job-9"]
        self.queue.get_payload.return_value = {
            "job_type": IngestJobType.FILE_UPLOAD,
            "plugin_id": "plugin-a",
            "document_id": 73,
        }
        self.repository.get_job.return_value = job

        self.assertEqual(self.worker.recover_interrupted(), 0)

        self.repository.recover_interrupted.assert_not_called()
        self.repository.mark_failed.assert_called_once_with(
            "job-9", "worker interrupted; retry limit reached"
        )
        document_id, cause = self.document_upload_service.fail_prepared_upload.call_args[0]
        self.assertEqual(document_id, 73)
        self.assertIsInstance(cause, RuntimeError)

    def test_recovery_requeues_job_below_retry_limit(self) -> None:
        job = Mock(
            id="job-9",
            plugin_id="plugin-a",
            status=IngestJobStatus.RUNNING,
            attempt_count=1,
            job_type=IngestJobType.WEB_CLIP,
            document_id=None,
        )
        self.queue.list_processing.return_value = ["job-9"]
        self.queue.get_payload.return_value = {
            "job_type": IngestJobType.WEB_CLIP,
            "plugin_id": "plugin-a",
        }
        self.repository.get_job.return_value = job
        self.repository.recover_interrupted.return_value = True

        self.assertEqual(self.worker.recover_interrupted(), 1)

        self.repository.recover_interrupted.assert_called_once_with("job-9")
        self.repository.mark_failed.assert_not_called()
        self.document_upload_service.fail_prepared_upload.assert_not_called()
        self.queue.recover_processing.assert_called_once_with()

    def test_recovery_without_payload_falls_back_to_plain_recover(self) -> None:
        """payload 过期时无法判定归属与次数，维持原有的直接恢复。"""
        self.queue.list_processing.return_value = ["job-9"]
        self.queue.get_payload.return_value = None
        self.repository.recover_interrupted.return_value = True

        self.assertEqual(self.worker.recover_interrupted(), 1)

        self.repository.get_job.assert_not_called()
        self.repository.mark_failed.assert_not_called()

    def test_terminal_write_conflict_leaves_job_unacknowledged(self) -> None:
        """回归：mark_failed 抛兄弟类冲突异常曾外溢杀掉 Worker；现在只保留 processing。"""
        job = Mock(
            id="job-3",
            plugin_id="plugin-a",
            job_type=IngestJobType.WEB_CLIP,
            document_id=None,
        )
        self.queue.dequeue.return_value = "job-3"
        self.repository.claim_job.return_value = job
        self.queue.get_payload.return_value = None
        self.repository.mark_failed.side_effect = IngestJobConflictError(
            "only RUNNING jobs can finish"
        )

        self.assertTrue(self.worker.run_once(timeout_seconds=0))

        self.repository.mark_failed.assert_called_once()
        self.queue.acknowledge.assert_not_called()

    def test_document_failure_write_error_still_records_job_failure(self) -> None:
        job = Mock(
            id="job-4",
            plugin_id="plugin-a",
            job_type=IngestJobType.FILE_UPLOAD,
            document_id=73,
        )
        self.queue.dequeue.return_value = "job-4"
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
        self.document_upload_service.process_upload.side_effect = RuntimeError("boom")
        self.document_upload_service.fail_prepared_upload.side_effect = RuntimeError(
            "document row locked"
        )

        self.assertTrue(self.worker.run_once(timeout_seconds=0))

        self.repository.mark_failed.assert_called_once_with("job-4", "boom")
        self.queue.acknowledge.assert_called_once_with("job-4")

    def test_recovery_ignores_terminal_write_conflict(self) -> None:
        job = Mock(
            id="job-9",
            plugin_id="plugin-a",
            status=IngestJobStatus.RUNNING,
            attempt_count=5,
            job_type=IngestJobType.WEB_CLIP,
            document_id=None,
        )
        self.queue.list_processing.return_value = ["job-9"]
        self.queue.get_payload.return_value = {
            "job_type": IngestJobType.WEB_CLIP,
            "plugin_id": "plugin-a",
        }
        self.repository.get_job.return_value = job
        self.repository.mark_failed.side_effect = IngestJobConflictError(
            "only RUNNING jobs can finish"
        )

        self.assertEqual(self.worker.recover_interrupted(), 0)

        self.queue.recover_processing.assert_called_once_with()

    def test_main_loop_survives_sibling_error_and_exits_only_on_interrupt(self) -> None:
        """主循环必须吞掉任务级异常继续跑，只有中断信号才让进程退出。"""
        worker = Mock(spec=IngestWorker)
        worker.recover_interrupted.return_value = 0
        worker.run_once.side_effect = [
            IngestJobConflictError("only RUNNING jobs can finish"),
            KeyboardInterrupt(),
        ]
        with (
            patch("backend.workers.ingest_worker.get_milvus_initializer"),
            patch("backend.workers.ingest_worker.IngestWorker", return_value=worker),
            patch("backend.workers.ingest_worker.time.sleep") as sleep,
        ):
            ingest_worker.main()

        self.assertEqual(worker.run_once.call_count, 2)
        sleep.assert_called_once_with(2)
