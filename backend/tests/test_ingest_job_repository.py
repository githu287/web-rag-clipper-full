from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from backend.core.exceptions import IngestJobConflictError, IngestJobNotFoundError
from backend.models.base import Base
from backend.models.ingest_job import IngestJobStatus, IngestJobType
from backend.models.plugin import PluginWorkspace
from backend.repositories.mysql.ingest_job_impl import IngestJobRepositoryImpl


class IngestJobRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            connection.execute(
                PluginWorkspace.__table__.insert().values(
                    plugin_id="plugin-a",
                    plugin_name="Plugin A",
                    plugin_name_norm="plugin a",
                    plugin_secret_hash="a" * 64,
                    status="ACTIVE",
                )
            )
        self.repo = IngestJobRepositoryImpl(self.engine)

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_lifecycle_and_duplicate_claim(self) -> None:
        job = self.repo.create_job("plugin-a", IngestJobType.WEB_CLIP)
        self.assertEqual(job.status, IngestJobStatus.QUEUED)
        self.assertEqual(job.progress, 0)

        claimed = self.repo.claim_job(job.id)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.status, IngestJobStatus.RUNNING)
        self.assertEqual(claimed.attempt_count, 1)
        self.assertIsNone(self.repo.claim_job(job.id))

        self.assertTrue(self.repo.recover_interrupted(job.id))
        claimed = self.repo.claim_job(job.id)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.attempt_count, 2)

        succeeded = self.repo.mark_succeeded(job.id, 123)
        self.assertEqual(succeeded.status, IngestJobStatus.SUCCEEDED)
        self.assertEqual(succeeded.progress, 100)
        self.assertEqual(succeeded.document_id, 123)

    def test_failure_requeue_and_ownership(self) -> None:
        job = self.repo.create_job("plugin-a", IngestJobType.WEB_CLIP)
        self.repo.claim_job(job.id)
        failed = self.repo.mark_failed(job.id, "boom")
        self.assertEqual(failed.status, IngestJobStatus.FAILED)
        self.assertEqual(failed.error_message, "boom")

        queued = self.repo.requeue(job.id, "plugin-a")
        self.assertEqual(queued.status, IngestJobStatus.QUEUED)
        self.assertIsNone(queued.error_message)
        with self.assertRaises(IngestJobConflictError):
            self.repo.requeue(job.id, "plugin-a")
        with self.assertRaises(IngestJobNotFoundError):
            self.repo.get_job(job.id, "plugin-b")

    def test_file_upload_job_keeps_document_reference(self) -> None:
        from backend.models.document import Document

        with self.engine.begin() as connection:
            result = connection.execute(
                Document.__table__.insert().values(
                    plugin_id="plugin-a",
                    filename="note.md",
                    file_path="stored-note.md",
                    source_type="upload",
                )
            )
            document_id = result.inserted_primary_key[0]

        job = self.repo.create_job(
            "plugin-a",
            IngestJobType.FILE_UPLOAD,
            document_id=document_id,
        )

        self.assertEqual(job.job_type, IngestJobType.FILE_UPLOAD)
        self.assertEqual(job.document_id, document_id)
