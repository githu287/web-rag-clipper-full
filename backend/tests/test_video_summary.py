"""Video queue/ownership/cleanup plus real FFmpeg media, with a fake cloud boundary."""
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.pool import StaticPool

from backend.api.deps import get_current_plugin
from backend.core.config import Settings
from backend.core.di import get_video_service
from backend.main import create_app
from backend.models.base import Base
from backend.models.plugin import PluginWorkspace
from backend.models.video import VideoJob
from backend.repositories.mysql.video import VideoRepository
from backend.core.video_errors import VideoError
from backend.services.video_links import normalize_link, public_addresses, choose_formats
from backend.services.video_media import ffmpeg_executable, inspect_video
from backend.services.video_summary import VideoSummaryService, DEFAULT_URL, job_response
from backend.workers.video_worker import VideoWorker


@pytest.fixture
def service(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    repository = VideoRepository(engine)
    with repository.sessions.begin() as session:
        for owner in ("a", "b"):
            session.add(PluginWorkspace(plugin_id=owner, plugin_name=owner, plugin_name_norm=owner, plugin_secret_hash=owner * 64))
    settings = Settings(upload_dir=str(tmp_path / "uploads"), app_master_key="1" * 32, video_max_upload_bytes=1048576, video_segment_seconds=10)
    service = VideoSummaryService(repository, settings)
    yield service
    engine.dispose()


def configure(service, owner="a"):
    return service.put_config(owner, {"api_key": "test-video-key-not-real", "base_url": DEFAULT_URL, "model": "qwen3-omni-flash"})


def test_video_configuration_is_separate_encrypted_and_never_returned(service):
    public = configure(service)
    assert public["configured"] and "api_key" not in public
    row = service.repo.config("a")
    assert "test-video-key" not in row.ciphertext
    assert service.config("a", secret=True)["api_key"] == "test-video-key-not-real"
    configure(service, "b")
    service.put_config("a", {"api_key": "", "base_url": DEFAULT_URL, "model": "qwen3.5-omni-flash"})
    assert service.config("a", secret=True)["api_key"] == "test-video-key-not-real"
    with service.repo.sessions() as session:
        assert session.scalar(select(PluginWorkspace).where(PluginWorkspace.plugin_id == "a")).api_key_ciphertext is None
    service.repo.delete_config("a")
    assert not service.config("a")["configured"] and service.config("b")["configured"]


@pytest.mark.parametrize("url", ["https://127.0.0.1/compatible-mode/v1", "https://dashscope.aliyuncs.com.evil.test/compatible-mode/v1", "http://dashscope.aliyuncs.com/compatible-mode/v1", "https://secret@dashscope.aliyuncs.com/compatible-mode/v1", DEFAULT_URL + "?key=secret"])
def test_config_rejects_unsafe_endpoints_without_echoing_key(service, url):
    with pytest.raises(VideoError) as caught:
        service.put_config("a", {"api_key": "private-value", "base_url": url, "model": "qwen3-omni-flash"})
    assert "private-value" not in str(caught.value)


def test_upload_bound_and_failure_remove_files_and_rows(service):
    configure(service)
    with pytest.raises(VideoError, match="超过"):
        service.upload("a", io.BytesIO(b"x" * (1048576 + 1)), "video.mp4")
    assert not service.repo.list("a")
    assert not list(service.root.glob("*/source.bin"))
    with pytest.raises(VideoError):
        service.upload("a", io.BytesIO(b""), "empty.mp4")
    with pytest.raises(VideoError):
        service.upload("a", io.BytesIO(b"test"), "playlist.m3u8")


def test_authentication_required_without_override(service):
    app = create_app()
    app.dependency_overrides[get_video_service] = lambda: service
    response = TestClient(app).get("/videos")
    assert response.status_code == 401


def test_api_validation_owner_filter_and_no_implicit_document(service):
    configure(service)
    app = create_app()
    current = SimpleNamespace(plugin_id="a")
    app.dependency_overrides[get_current_plugin] = lambda: current
    app.dependency_overrides[get_video_service] = lambda: service
    client = TestClient(app)
    response = client.post("/videos/upload", files={"file": ("sample.mp4", b"real-video-tested-separately", "video/mp4")})
    assert response.status_code == 202
    job_id = response.json()["id"]
    assert "plugin_id" not in response.json()
    assert client.get("/videos/config").json()["configured"]
    bad = client.put("/videos/config", json={"api_key": "private-value", "base_url": "unsafe", "model": "fake"})
    assert bad.status_code == 422 and "private-value" not in bad.text
    assert client.put("/videos/config", content=b"x" * 5000).status_code == 413
    current.plugin_id = "b"
    for method, suffix in (("get", ""), ("delete", ""), ("post", "/retry"), ("post", "/cancel")):
        assert getattr(client, method)("/videos/" + job_id + suffix).status_code == 404
    assert client.get("/videos").json() == {"items": []}
    current.plugin_id = "a"
    assert client.post(f"/videos/{job_id}/cancel").json()["status"] == "CANCELLED"
    assert not service.input_path(job_id).exists()
    assert client.post(f"/videos/{job_id}/retry").status_code == 409
    assert client.delete(f"/videos/{job_id}").status_code == 204
    with service.repo.sessions() as session:
        from backend.models.document import Document
        assert list(session.scalars(select(Document))) == []


def test_durable_claim_lease_and_interrupted_manual_retry(service):
    configure(service)
    job = service.upload("a", io.BytesIO(b"bytes"), "a.mp4")
    claimed = service.repo.claim()
    assert claimed.id == job.id and claimed.attempts == 1
    assert service.repo.claim() is None
    assert service.repo.heartbeat(claimed, stage="云端处理")
    with service.repo.sessions.begin() as session:
        session.execute(update(VideoJob).where(VideoJob.id == job.id).values(lease_until=datetime.utcnow() - timedelta(seconds=1)))
    assert service.repo.claim() is None
    assert service.repo.get("a", job.id).status == "FAILED"
    service.retry("a", job.id)
    new = service.repo.claim()
    assert new.attempts == 2 and new.lease != claimed.lease
    assert not service.repo.finish(claimed, result="stale")
    assert service.repo.finish(new, error="model unavailable")
    service.retry("a", job.id)
    third = service.repo.claim()
    service.repo.finish(third, error="failed")
    with pytest.raises(VideoError):
        service.retry("a", job.id)


def test_job_quota_and_expiry_cleanup(service):
    configure(service)
    jobs = [service.upload("a", io.BytesIO(b"bytes"), "a.mp4") for _ in range(3)]
    with pytest.raises(VideoError) as caught:
        service.prepare("a", "another.mp4")
    assert caught.value.status == 429
    with service.repo.sessions.begin() as session:
        session.execute(update(VideoJob).where(VideoJob.id == jobs[0].id).values(expires_at=datetime.utcnow() - timedelta(seconds=1)))
    service.cleanup()
    assert len(service.repo.list("a")) == 2
    assert not service.directory(jobs[0].id).exists()


def test_workspace_delete_fences_worker_and_cleans_video(service):
    configure(service)
    job = service.upload("a", io.BytesIO(b"bytes"), "a.mp4")
    claimed = service.repo.claim()
    service.clear_workspace("a")
    assert not service.repo.heartbeat(claimed)
    assert not service.directory(job.id).exists()
    with pytest.raises(VideoError):
        configure(service)
    service.repo.resume_workspace("a")
    assert configure(service)["configured"]


def test_removing_cloud_credentials_cancels_future_calls_and_cleans_inputs(service):
    configure(service)
    job = service.upload("a", io.BytesIO(b"bytes"), "a.mp4")
    worker = service.repo.claim()
    service.remove_config("a")
    assert service.repo.get("a", job.id).status == "CANCELLED"
    assert not service.repo.heartbeat(worker)
    assert not service.input_path(job.id).exists()
    assert not service.config("a")["configured"]


@pytest.mark.parametrize("link", ["https://127.0.0.1/video/123", "https://www.bilibili.com.evil.test/video/BV123", "https://u:p@www.bilibili.com/video/BV123", "file:///etc/passwd", "https://www.bilibili.com:8080/video/BV123", "https://www.douyin.com/user/123", "https://www.bilibili.com/bangumi/play/ep123"])
def test_platform_input_restrictions(link):
    with pytest.raises(VideoError):
        normalize_link(link)


def test_domestic_share_links_and_part_are_preserved():
    assert normalize_link("分享视频 https://v.douyin.com/Abc_12/ 复制打开") == "https://v.douyin.com/Abc_12/"
    assert normalize_link("https://www.bilibili.com/video/BV123?p=3&tracking=secret") == "https://www.bilibili.com/video/BV123?p=3"
    assert normalize_link("https://www.iesdouyin.com/share/video/123/?foo=1") == "https://www.douyin.com/video/123"


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["10.0.0.1"], ["169.254.169.254"], ["::1"], ["93.184.216.34", "192.168.1.1"]])
def test_dns_rebinding_and_internal_addresses_are_blocked(addresses):
    with patch("backend.services.video_links.socket.getaddrinfo", return_value=[(2, 1, 6, "", (value, 443)) for value in addresses]):
        with pytest.raises(VideoError):
            public_addresses("www.bilibili.com")


def test_format_selection_keeps_audio_and_avoids_playlists():
    video = {"url": "https://x.bilivideo.com/a.mp4", "ext": "mp4", "protocol": "https", "vcodec": "avc1", "acodec": "none", "height": 720}
    audio = {"url": "https://x.bilivideo.com/a.m4a", "ext": "m4a", "protocol": "https", "vcodec": "none", "acodec": "aac", "abr": 64}
    assert choose_formats({"formats": [video, audio]}) == [video, audio]
    with pytest.raises(VideoError):
        choose_formats({"formats": [video]})
    with pytest.raises(VideoError):
        choose_formats({"formats": [{**video, "protocol": "m3u8_native"}, audio]})


def test_platform_selects_a_smaller_format_including_the_audio_budget():
    video = {"url": "https://x.bilivideo.com/a.mp4", "ext": "mp4", "protocol": "https", "vcodec": "avc1", "acodec": "none", "height": 720, "filesize": 95}
    smaller = {**video, "vcodec": "av01", "filesize": 40}
    audio = {"url": "https://x.bilivideo.com/a.m4a", "ext": "m4a", "protocol": "https", "vcodec": "none", "acodec": "aac", "abr": 64, "filesize": 10}
    assert choose_formats({"formats": [video, smaller, audio]}, 100) == [smaller, audio]
    with pytest.raises(VideoError, match="大小"):
        choose_formats({"formats": [video, smaller, audio]}, 49)


@pytest.fixture
def real_video(service, tmp_path):
    pytest.importorskip("imageio_ffmpeg")
    source = tmp_path / "actual.mp4"
    subprocess.run([ffmpeg_executable(service.settings), "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=160x120:r=12", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000", "-t", "11", "-c:v", "libx264", "-c:a", "aac", str(source)], check=True, timeout=30)
    return source


def test_real_media_and_durable_worker_pipeline(service, real_video):
    configure(service)
    info = inspect_video(real_video, service.settings)
    assert info.has_audio and 10.9 <= info.duration <= 11.2
    with real_video.open("rb") as source:
        job = service.upload("a", source, "actual.mp4")
    cloud = Mock()
    cloud.summarize.return_value = ("画面为蓝色背景，声音是连续音调。", {"total_tokens": 12})
    cloud.combine.return_value = ("蓝色背景配连续音调，无语音叙述。", {"total_tokens": 4})
    worker = VideoWorker(service, cloud)
    assert worker.run_once()
    result = job_response(service.repo.get("a", job.id))
    assert result["status"] == "SUCCEEDED"
    assert len(result["result"]["chapters"]) == 2
    assert result["result"]["chapters"][1]["start_seconds"] == 10
    assert result["result"]["usage"]["total_tokens"] == 28
    assert result["result"]["timestamp_basis"] == "segment_boundaries"
    assert not service.input_path(job.id).exists()
    assert cloud.summarize.call_count == 2 and cloud.combine.call_count == 1
    assert not worker.run_once()
    assert not list(service.root.iterdir())


def test_corrupt_media_never_calls_model(service):
    configure(service)
    job = service.upload("a", io.BytesIO(b"#EXTM3U\nhttp://127.0.0.1"), "fake.mp4")
    cloud = Mock()
    VideoWorker(service, cloud).run_once()
    assert service.repo.get("a", job.id).status == "FAILED"
    cloud.summarize.assert_not_called()


def test_silent_video_gets_bounded_silent_audio_but_retains_original_metadata(service, real_video, tmp_path):
    silent = tmp_path / "silent.mp4"
    subprocess.run([ffmpeg_executable(service.settings), "-nostdin", "-loglevel", "error", "-y",
                    "-i", str(real_video), "-an", "-c:v", "copy", str(silent)], check=True, timeout=30)
    assert not inspect_video(silent, service.settings).has_audio
    configure(service)
    with silent.open("rb") as stream:
        job = service.upload("a", stream, "silent.mp4")
    lengths = []
    def summarize(segment, *_args):
        info = inspect_video(segment, service.settings)
        assert info.has_audio
        lengths.append(info.duration)
        # Decode the actual generated audio; padding must be digital silence.
        audio = subprocess.run([ffmpeg_executable(service.settings), "-nostdin", "-loglevel", "error",
                                "-i", str(segment), "-map", "0:a:0", "-f", "s16le", "-"],
                               capture_output=True, check=True, timeout=30).stdout
        assert audio and not any(audio)
        return "蓝色画面，无讲解", {}
    cloud = Mock(summarize=Mock(side_effect=summarize))
    cloud.combine.return_value = ("蓝色画面，无讲解", {})
    VideoWorker(service, cloud).run_once()
    result = job_response(service.repo.get("a", job.id))
    assert result["status"] == "SUCCEEDED"
    assert result["result"]["has_audio"] is False
    assert "未检测到音轨" in result["result"]["basis"]
    assert len(lengths) == 2 and 9.8 <= lengths[0] <= 10.2 and 0.8 <= lengths[1] <= 1.2
    assert not list(service.root.iterdir())


def test_cancelled_worker_cannot_write_result(service, real_video):
    configure(service)
    with real_video.open("rb") as stream:
        job = service.upload("a", stream, "actual.mp4")
    def summarize(*_args):
        service.repo.cancel("a", job.id)
        return "late provider response", {}
    cloud = Mock(summarize=Mock(side_effect=summarize))
    VideoWorker(service, cloud).run_once()
    current = service.repo.get("a", job.id)
    assert current.status == "CANCELLED" and current.result is None
    assert not service.input_path(job.id).exists()


def test_multipart_limit_applies_before_spooling_with_unknown_size(service):
    from backend.api.video_body_limit import VideoBodyLimit
    app = create_app()
    app.dependency_overrides[get_video_service] = lambda: service
    app.dependency_overrides[get_current_plugin] = lambda: SimpleNamespace(plugin_id="a")
    client = TestClient(VideoBodyLimit(app, max_bytes=1024))
    data = b"--boundary\r\nContent-Disposition: form-data; name=\"file\"; filename=\"huge.mp4\"\r\nContent-Type: video/mp4\r\n\r\n" + b"x" * 1100000 + b"\r\n--boundary--\r\n"
    response = client.post("/videos/upload", content=iter([data[:100], data[100:]]), headers={"Content-Type": "multipart/form-data; boundary=boundary"})
    assert response.status_code == 413
    assert not service.repo.list("a")
    response = client.post("/videos/upload", content=b"", headers={"Content-Length": "1100000"})
    assert response.status_code == 413


def test_video_migration_round_trip_preserves_workspace(service):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    import importlib.util
    path = Path(__file__).resolve().parents[2] / "alembic/versions/0013_video_summary.py"
    spec = importlib.util.spec_from_file_location("video_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = service.repo.sessions.kw["bind"]
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()
            module.upgrade()
            checkpoint_path = path.with_name("0014_video_processing_state.py")
            checkpoint_spec = importlib.util.spec_from_file_location("video_checkpoint_migration", checkpoint_path)
            checkpoint_module = importlib.util.module_from_spec(checkpoint_spec)
            checkpoint_spec.loader.exec_module(checkpoint_module)
            checkpoint_module.upgrade()
    configure(service)
    assert service.repo.create("a", "video", "https://www.douyin.com/video/123", 24).status == "QUEUED"
