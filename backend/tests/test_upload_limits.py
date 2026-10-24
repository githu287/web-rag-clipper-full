"""Bound upload reads and keep slow upload requests off the API event loop."""

import asyncio
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient

from backend.api.deps import get_current_plugin
from backend.api.upload import read_upload_content
from backend.core.di import (
    get_document_upload_service, get_ingest_job_service, get_plugin_service, get_settings,
)
from backend.core.exceptions import ApiKeyNotConfiguredError, DocumentFileTooLargeError, IngestJobOperationError
from backend.main import create_app


@pytest.fixture
def upload_app(monkeypatch):
    app = create_app()
    document = SimpleNamespace(id=7, filename="note.txt", file_size=8, mime_type="text/plain",
                               status="SUCCESS", chunk_count=1, error_message=None)
    job = SimpleNamespace(id="job-1", job_type="FILE_UPLOAD", status="QUEUED", stage="QUEUED",
                          progress=0, attempt_count=0, document_id=7, error_message=None,
                          created_at=datetime.now(), updated_at=datetime.now(), started_at=None, finished_at=None)
    upload = Mock(prepare_upload=Mock(return_value=document), upload=AsyncMock(return_value=document))
    jobs = Mock(create_file_upload_job=Mock(return_value=job))
    plugins = Mock(decrypt_api_key=Mock(return_value="test-key"))
    app.dependency_overrides[get_current_plugin] = lambda: SimpleNamespace(plugin_id="a")
    app.dependency_overrides[get_settings] = lambda: SimpleNamespace(max_page_content_bytes=8)
    app.dependency_overrides[get_document_upload_service] = lambda: upload
    app.dependency_overrides[get_ingest_job_service] = lambda: jobs
    app.dependency_overrides[get_plugin_service] = lambda: plugins
    monkeypatch.setattr("backend.main.get_milvus_initializer", lambda: Mock())

    @app.get("/test/ping")
    async def ping():
        return {"ok": True}

    yield SimpleNamespace(app=app, upload=upload, jobs=jobs, plugins=plugins, document=document, job=job)
    app.dependency_overrides.clear()


@pytest.mark.parametrize("path", ["/documents/upload", "/documents/upload/async"])
@pytest.mark.parametrize("content", [b"x" * 9, b"x" * 4096, "汉字汉".encode("utf-8")], ids=["one-over", "large", "utf8"])
def test_oversized_multipart_is_rejected_before_storage_or_job_creation(upload_app, path, content):
    with TestClient(upload_app.app) as client:
        response = client.post(path, files={"file": ("note.txt", content, "text/plain")})
    assert response.status_code == 413
    assert response.json()["type"] == "DocumentFileTooLargeError"
    upload_app.upload.prepare_upload.assert_not_called()
    upload_app.upload.upload.assert_not_awaited()
    upload_app.jobs.create_file_upload_job.assert_not_called()


@pytest.mark.parametrize("path,status", [("/documents/upload", 201), ("/documents/upload/async", 202)])
def test_exact_byte_limit_is_accepted_without_truncation(upload_app, path, status):
    content = "汉字ab".encode("utf-8")
    with TestClient(upload_app.app) as client:
        response = client.post(path, files={"file": ("note.txt", content, "text/plain")})
    assert response.status_code == status
    method = upload_app.upload.upload if status == 201 else upload_app.upload.prepare_upload
    assert method.call_args.kwargs["content"] == content


@pytest.mark.parametrize("boundary", ["prepare", "publish", "compensate", "ingest"])
def test_slow_upload_operation_does_not_block_other_requests(upload_app, boundary):
    started, release = threading.Event(), threading.Event()

    def slow(result):
        started.set()
        if not release.wait(2):
            raise AssertionError("upload blocked unrelated API requests")
        return result

    path, expected = "/documents/upload/async", 202
    if boundary == "prepare":
        upload_app.upload.prepare_upload.side_effect = lambda **kwargs: slow(upload_app.document)
    elif boundary == "publish":
        upload_app.jobs.create_file_upload_job.side_effect = lambda *args: slow(upload_app.job)
    elif boundary == "compensate":
        upload_app.jobs.create_file_upload_job.side_effect = IngestJobOperationError("database unavailable")
        upload_app.upload.fail_prepared_upload.side_effect = lambda *args, **kwargs: slow(None)
        expected = 503
    else:
        async def ingest(**kwargs):
            return slow(upload_app.document)
        upload_app.upload.upload.side_effect = ingest
        path, expected = "/documents/upload", 201

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=upload_app.app), base_url="http://test") as client:
            task = asyncio.create_task(client.post(path, files={"file": ("note.txt", b"content", "text/plain")}))
            try:
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.01)
                assert started.is_set()
                if task.done():
                    await task  # Retrieve a blocking-operation failure, if any.
                assert not task.done()
                response = await client.get("/test/ping")
                assert response.json() == {"ok": True}
            finally:
                release.set()
            assert (await task).status_code == expected

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["/documents/upload", "/documents/upload/async"])
def test_missing_model_configuration_creates_no_upload_or_job(upload_app, path):
    upload_app.plugins.decrypt_api_key.side_effect = ApiKeyNotConfiguredError("model credentials missing")
    with TestClient(upload_app.app) as client:
        response = client.post(path, files={"file": ("note.txt", b"content", "text/plain")})
    assert response.status_code == 409
    upload_app.upload.prepare_upload.assert_not_called()
    upload_app.upload.upload.assert_not_awaited()
    upload_app.jobs.create_file_upload_job.assert_not_called()


class CountingStream:
    """A virtual large file which records how many bytes the reader consumes."""
    def __init__(self, size, short_read=None):
        self.remaining = size
        self.consumed = 0
        self.requests = []
        self.short_read = short_read

    def read(self, size):
        assert size > 0, "unbounded or zero-length read"
        self.requests.append(size)
        taken = min(size, self.remaining, self.short_read or size)
        self.remaining -= taken
        self.consumed += taken
        return b"x" * taken


@pytest.mark.parametrize("reported_size", [None, 0, 2])
@pytest.mark.parametrize("short_read", [None, 3])
def test_unknown_or_underreported_large_file_consumes_only_limit_plus_one(reported_size, short_read):
    stream = CountingStream(10_000_000, short_read)
    upload = UploadFile(stream, filename="note.txt", size=reported_size)
    with pytest.raises(DocumentFileTooLargeError):
        read_upload_content(upload, 8)
    assert stream.consumed == 9
    assert stream.remaining == 10_000_000 - 9


def test_known_oversized_file_is_rejected_without_reading():
    stream = CountingStream(10_000_000)
    with pytest.raises(DocumentFileTooLargeError):
        read_upload_content(UploadFile(stream, filename="note.txt", size=10_000_000), 8)
    assert stream.requests == []


@pytest.mark.parametrize("size", [0, 7, 8, 65536, 65537])
def test_short_reads_preserve_all_bytes_within_limit(size):
    stream = CountingStream(size, short_read=1024)
    content = read_upload_content(UploadFile(stream, filename="note.txt"), max(8, size))
    assert content == b"x" * size
    assert stream.consumed == size
    assert max(stream.requests) <= 65536


def test_stopped_caller_does_not_interrupt_prepare_to_job_submission(upload_app):
    started, release, published = threading.Event(), threading.Event(), threading.Event()

    def prepare(**kwargs):
        started.set()
        if not release.wait(2):
            raise AssertionError("test did not release upload preparation")
        return upload_app.document

    def publish(*args):
        published.set()
        return upload_app.job

    upload_app.upload.prepare_upload.side_effect = prepare
    upload_app.jobs.create_file_upload_job.side_effect = publish

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=upload_app.app), base_url="http://test") as client:
            task = asyncio.create_task(client.post("/documents/upload/async", files={"file": ("note.txt", b"content", "text/plain")}))
            try:
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.01)
                assert started.is_set()
                task.cancel()
                await asyncio.sleep(0)
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            for _ in range(100):
                if published.is_set():
                    break
                await asyncio.sleep(0.01)
            assert published.is_set()

    asyncio.run(scenario())
    upload_app.jobs.create_file_upload_job.assert_called_once_with("a", 7)
