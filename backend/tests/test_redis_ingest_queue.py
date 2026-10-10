from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from backend.core.config import Settings
from backend.core.exceptions import IngestJobOperationError
from backend.tasks.redis_queue import RedisIngestQueue


class RedisIngestQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = MagicMock()
        patcher = patch("backend.tasks.redis_queue.Redis", return_value=self.client)
        self.addCleanup(patcher.stop)
        self.redis_constructor = patcher.start()
        self.queue = RedisIngestQueue(
            Settings(
                ingest_queue_name="test:ingest",
                ingest_payload_ttl_seconds=600,
            )
        )

    def test_socket_timeout_exceeds_worker_blocking_timeout(self) -> None:
        kwargs = self.redis_constructor.call_args.kwargs
        self.assertGreater(kwargs["socket_timeout"], 5)

    def test_enqueue_writes_payload_and_queue_atomically(self) -> None:
        pipeline = MagicMock()
        self.client.pipeline.return_value.__enter__.return_value = pipeline
        self.queue.enqueue("job-1", {"plugin_id": "plugin-a", "raw_text": "正文"})
        pipeline.setex.assert_called_once()
        pipeline.lpush.assert_called_once_with("test:ingest", "job-1")
        pipeline.execute.assert_called_once_with()

    def test_dequeue_uses_processing_list_and_acknowledges(self) -> None:
        self.client.brpoplpush.return_value = "job-1"
        self.assertEqual(self.queue.dequeue(2), "job-1")
        self.client.brpoplpush.assert_called_once_with(
            "test:ingest", "test:ingest:processing", timeout=2
        )
        self.queue.acknowledge("job-1")
        self.client.lrem.assert_called_once_with(
            "test:ingest:processing", 1, "job-1"
        )

    def test_enqueue_existing_requeues_atomically_when_payload_exists(self) -> None:
        self.client.eval.return_value = 1

        self.queue.enqueue_existing("job-1")

        self.client.eval.assert_called_once()
        self.client.exists.assert_not_called()
        self.client.lpush.assert_not_called()

    def test_enqueue_existing_rejects_expired_payload(self) -> None:
        self.client.eval.return_value = 0

        with self.assertRaisesRegex(
            IngestJobOperationError, "payload has expired"
        ):
            self.queue.enqueue_existing("job-1")

        self.client.eval.assert_called_once()

    def test_recover_moves_all_processing_jobs_back(self) -> None:
        self.client.rpoplpush.side_effect = ["job-1", "job-2", None]
        self.assertEqual(self.queue.recover_processing(), ["job-1", "job-2"])
