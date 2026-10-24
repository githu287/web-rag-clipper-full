from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import Mock

from fastapi.testclient import TestClient

from backend.api.deps import get_current_plugin
from backend.core.di import (
    get_ingest_job_service,
    get_plugin_service,
    get_web_clip_service,
)
from backend.core.exceptions import (
    DocumentQuotaExceededError,
    IngestJobConflictError,
    IngestJobNotFoundError,
)
from backend.main import create_app
from backend.models.ingest_job import IngestJobStatus, IngestJobType
from backend.services.web_clip import WebClipService


def make_job(status: str = IngestJobStatus.QUEUED) -> Mock:
    now = datetime(2026, 9, 20, 12, 0, 0)
    job = Mock()
    job.id = "11111111-1111-1111-1111-111111111111"
    job.job_type = IngestJobType.WEB_CLIP
    job.status = status
    job.stage = status
    job.progress = 0 if status == IngestJobStatus.QUEUED else 100
    job.attempt_count = 0
    job.document_id = None
    job.error_message = None
    job.created_at = now
    job.updated_at = now
    job.started_at = None
    job.finished_at = None
    return job


class IngestJobApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.job_service = Mock()
        self.plugin_service = Mock()
        self.web_clip_service = Mock()
        self.plugin = Mock(plugin_id="plugin-a")
        self.app = create_app()
        self.app.dependency_overrides[get_current_plugin] = lambda: self.plugin
        self.app.dependency_overrides[get_ingest_job_service] = lambda: self.job_service
        self.app.dependency_overrides[get_plugin_service] = lambda: self.plugin_service
        self.app.dependency_overrides[get_web_clip_service] = (
            lambda: self.web_clip_service
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.app.dependency_overrides.clear()

    def test_submit_async_clip_returns_202_job(self) -> None:
        job = make_job()
        self.job_service.create_web_clip_job.return_value = job
        response = self.client.post(
            "/clips/async",
            json={"url": "https://example.com/", "title": "Example", "raw_text": "body"},
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["id"], job.id)
        self.assertEqual(response.json()["status"], "QUEUED")
        self.plugin_service.decrypt_api_key.assert_called_once_with(self.plugin)
        self.web_clip_service.ensure_capacity.assert_called_once_with("plugin-a", url="https://example.com/")
        self.job_service.create_web_clip_job.assert_called_once()

    def test_submit_async_clip_rejected_at_quota_429_without_job(self) -> None:
        """#24c：满额时提交即 429，不创建注定失败的任务、不写 Redis payload。"""
        self.web_clip_service.ensure_capacity.side_effect = (
            DocumentQuotaExceededError(
                "知识库文档数已达上限（188 篇），请先删除部分文档后再剪藏"
            )
        )

        response = self.client.post(
            "/clips/async",
            json={"url": "https://example.com/", "title": "Example", "raw_text": "body"},
        )

        self.assertEqual(response.status_code, 429)
        body = response.json()
        self.assertEqual(body["type"], "DocumentQuotaExceededError")
        self.assertIn("请先删除部分文档", body["detail"])
        self.job_service.create_web_clip_job.assert_not_called()

    def test_async_clip_at_full_quota_allows_existing_url_but_rejects_new_route(self) -> None:
        repository = Mock()
        repository.count_documents.return_value = 188
        repository.get_webpage_by_url.return_value = Mock(id=7)
        service = WebClipService(repository, Mock(), Mock(), max_documents_per_workspace=188)
        self.app.dependency_overrides[get_web_clip_service] = lambda: service
        self.job_service.create_web_clip_job.return_value = make_job()

        response = self.client.post("/clips/async", json={
            "url": "https://example.com/?utm_source=test#/one", "raw_text": "updated body",
        })
        self.assertEqual(response.status_code, 202)
        repository.get_webpage_by_url.assert_called_once_with("plugin-a", "https://example.com/#/one")
        repository.count_documents.assert_not_called()

        repository.get_webpage_by_url.return_value = None
        response = self.client.post("/clips/async", json={
            "url": "https://example.com/#/two", "raw_text": "new body",
        })
        self.assertEqual(response.status_code, 429)
        self.job_service.create_web_clip_job.assert_called_once()

    def test_get_and_retry_are_workspace_scoped(self) -> None:
        job = make_job(IngestJobStatus.FAILED)
        self.job_service.get_job.return_value = job
        self.job_service.retry_job.return_value = make_job(IngestJobStatus.QUEUED)
        response = self.client.get(f"/jobs/{job.id}")
        self.assertEqual(response.status_code, 200)
        self.job_service.get_job.assert_called_once_with(job.id, "plugin-a")
        response = self.client.post(f"/jobs/{job.id}/retry")
        self.assertEqual(response.status_code, 202)
        self.job_service.retry_job.assert_called_once_with(job.id, "plugin-a")

    def test_job_errors_map_to_404_and_409(self) -> None:
        self.job_service.get_job.side_effect = IngestJobNotFoundError("missing")
        response = self.client.get("/jobs/missing")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "INGEST_JOB_NOT_FOUND")

        self.job_service.retry_job.side_effect = IngestJobConflictError("running")
        response = self.client.post("/jobs/job-1/retry")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "INGEST_JOB_CONFLICT")
