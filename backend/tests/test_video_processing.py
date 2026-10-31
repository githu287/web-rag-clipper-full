"""Durable stage reuse, call uncertainty, fencing and cleanup contracts."""
from datetime import datetime, timedelta
from io import BytesIO, StringIO
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import inspect, update

from backend.core.video_errors import VideoError
from backend.core.video_processing import load_state, processing_view, reported_usage
from backend.models.video import VideoJob
from backend.repositories.mysql.video import VideoRepository
from backend.services.video_summary import VideoSummaryService, DEFAULT_URL, job_response
from backend.services.video_media import VideoInfo
from backend.workers.video_worker import VideoWorker
from backend.tests.test_video_summary import service, configure


@pytest.fixture
def media(monkeypatch):
    monkeypatch.setattr("backend.workers.video_worker.inspect_video", lambda *_: VideoInfo(11, True))
    def derive(source, output, *_args, **_kwargs): output.write_bytes(b"derived")
    monkeypatch.setattr("backend.workers.video_worker.make_audio", derive)
    monkeypatch.setattr("backend.workers.video_worker.make_segment", derive)


def setup(service, owner="a"):
    configure(service, owner)
    service.put_config(owner, {"base_url": DEFAULT_URL, "model": "qwen3-omni-flash", "asr_enabled": True})
    return service.upload(owner, BytesIO(b"same immutable test media"), "fixture.mp4")


def clients():
    cloud, asr = Mock(), Mock()
    cloud.summarize.return_value = ("private-summary", {"total_tokens": 10})
    cloud.combine.return_value = ("private-overview", {"total_tokens": 5})
    asr.transcribe.return_value = ([{"start_seconds": .1, "end_seconds": .8, "text": "private-asr"}], {"duration_seconds": 1})
    return cloud, asr


def test_retry_after_segment_failure_reuses_asr_and_summary_without_double_counting(service, media):
    job = setup(service)
    cloud, asr = clients()
    cloud.summarize.side_effect = [("first", {"total_tokens": 10}), VideoError("injected failure", 502)]
    VideoWorker(service, cloud, asr).run_once()
    failed = service.repo.get("a", job.id)
    view = job_response(failed)["processing"]
    assert failed.status == "FAILED" and view["completed"] == {"asr": 2, "summary": 1}
    assert view["known_usage"] == {"video": {"total_tokens": 10}, "asr": {"duration_seconds": 2}}
    assert view["unknown_calls"] == 1 and "private-asr" not in json.dumps(job_response(failed))
    # A new service/repository/worker instance observes only durable SQL state.
    restarted = VideoSummaryService(VideoRepository(service.repo.sessions.kw["bind"]), service.settings)
    restarted.retry("a", job.id)
    next_cloud, next_asr = clients()
    next_cloud.summarize.return_value = ("second", {"total_tokens": 20})
    VideoWorker(restarted, next_cloud, next_asr).run_once()
    done = service.repo.get("a", job.id)
    result, view = json.loads(done.result), processing_view(done.processing_state)
    assert done.status == "SUCCEEDED" and done.attempts == 2
    next_cloud.summarize.assert_called_once(); next_cloud.combine.assert_called_once(); next_asr.transcribe.assert_not_called()
    assert view["reused"] == {"asr": 2, "summary": 1}
    assert view["known_usage"]["video"]["total_tokens"] == result["usage"]["total_tokens"] == 35
    assert result["transcript"]["sentences"][1]["id"] == 2
    assert result["transcript"]["sentences"][1]["start_seconds"] == 10.1
    assert view["unknown_calls"] == 1 and not load_state(done.processing_state)["cache"]
    assert not list(service.root.iterdir())


def test_failed_overview_retries_only_overview(service, media):
    job = setup(service); cloud, asr = clients()
    cloud.combine.side_effect = VideoError("overview failure", 504)
    VideoWorker(service, cloud, asr).run_once()
    service.retry("a", job.id)
    next_cloud, next_asr = clients()
    VideoWorker(service, next_cloud, next_asr).run_once()
    next_cloud.summarize.assert_not_called(); next_asr.transcribe.assert_not_called()
    next_cloud.combine.assert_called_once()
    view = processing_view(service.repo.get("a", job.id).processing_state)
    assert view["reused"] == {"asr": 2, "summary": 2} and view["unknown_calls"] == 1


def test_overview_checkpoint_survives_final_result_commit_failure(service, media, monkeypatch):
    job = setup(service); cloud, asr = clients()
    finish = service.repo.finish
    def fail_result_once(job, **options):
        if options.get("result"): raise RuntimeError("injected commit failure")
        return finish(job, **options)
    monkeypatch.setattr(service.repo, "finish", fail_result_once)
    VideoWorker(service, cloud, asr).run_once()
    monkeypatch.setattr(service.repo, "finish", finish)
    service.retry("a", job.id)
    next_cloud, next_asr = clients()
    VideoWorker(service, next_cloud, next_asr).run_once()
    next_cloud.summarize.assert_not_called(); next_cloud.combine.assert_not_called(); next_asr.transcribe.assert_not_called()
    assert service.repo.get("a", job.id).status == "SUCCEEDED"


@pytest.mark.parametrize("change", ["source", "model", "key", "asr", "geometry", "preprocessing", "media_tool"])
def test_material_or_configuration_changes_invalidate_cache_but_keep_prior_usage(service, media, monkeypatch, change):
    job = setup(service); cloud, asr = clients()
    cloud.summarize.side_effect = VideoError("failed first segment", 502)
    VideoWorker(service, cloud, asr).run_once()
    if change == "source": service.input_path(job.id).write_bytes(b"different material")
    if change == "model": service.put_config("a", {"base_url": DEFAULT_URL, "model": "qwen3.5-omni-flash"})
    if change == "key": service.put_config("a", {"base_url": DEFAULT_URL, "model": "qwen3-omni-flash", "api_key": "different-fixture-key"})
    if change == "asr": service.put_config("a", {"base_url": DEFAULT_URL, "model": "qwen3-omni-flash", "asr_enabled": False})
    if change == "geometry": service.settings.video_segment_seconds = 11
    if change == "media_tool": service.settings.video_ffmpeg = "different-test-media-tool"
    if change == "preprocessing": monkeypatch.setattr("backend.core.video_processing.PIPELINE_VERSION", 2)
    service.retry("a", job.id)
    next_cloud, next_asr = clients()
    VideoWorker(service, next_cloud, next_asr).run_once()
    done = service.repo.get("a", job.id); view = processing_view(done.processing_state)
    assert done.status == "SUCCEEDED" and view["reused"] == {}
    assert view["known_usage"]["asr"]["duration_seconds"] >= 1  # First attempt remains in the ledger.
    assert next_cloud.summarize.call_count >= 1
    assert "fixture-key" not in done.processing_state and "private-asr" not in done.processing_state


def test_crashed_call_is_unknown_manual_retry_keeps_successes_and_fences_old_worker(service, media):
    job = setup(service); old = service.repo.claim()
    service.repo.begin_processing(old, "same-context")
    first = service.repo.start_call(old, "summary", 0)
    service.repo.complete_call(old, first, "summary:0", data="private", usage={"total_tokens": 8})
    lost = service.repo.start_call(old, "summary", 1)
    with service.repo.sessions.begin() as session:
        session.execute(update(VideoJob).where(VideoJob.id == job.id).values(lease_until=datetime.utcnow() - timedelta(seconds=1)))
    assert service.repo.claim() is None  # A crash never silently sends another paid call.
    failed = service.repo.get("a", job.id)
    assert failed.status == "FAILED" and processing_view(failed.processing_state)["unknown_calls"] == 1
    assert load_state(failed.processing_state)["calls"][-1]["status"] == "interrupted"
    service.retry("a", job.id); new = service.repo.claim()
    assert service.repo.begin_processing(new, "same-context")["summary:0"]["data"] == "private"
    with pytest.raises(VideoError): service.repo.complete_call(old, lost, "summary:1", data="stale")
    assert "stale" not in service.repo.get("a", job.id).processing_state


@pytest.mark.parametrize("operation", ["cancel", "remove_config", "delete", "expire", "workspace"])
def test_cancel_delete_expiry_and_workspace_removal_clear_private_checkpoints(service, media, operation):
    job = setup(service); claimed = service.repo.claim()
    service.repo.begin_processing(claimed, "context")
    token = service.repo.start_call(claimed, "summary", 0)
    service.repo.complete_call(claimed, token, "summary:0", data="private-checkpoint", usage={"total_tokens": 3})
    pending = service.repo.start_call(claimed, "asr", 0)
    if operation == "cancel": service.cancel("a", job.id)
    if operation == "remove_config": service.remove_config("a")
    if operation == "delete": service.delete("a", job.id)
    if operation == "workspace": service.clear_workspace("a")
    if operation == "expire":
        with service.repo.sessions.begin() as session:
            session.execute(update(VideoJob).where(VideoJob.id == job.id).values(expires_at=datetime.utcnow() - timedelta(seconds=1)))
        service.cleanup()
    with pytest.raises(VideoError): service.repo.complete_call(claimed, pending, "asr:0", data=[{"text": "late"}])
    if operation in ("cancel", "remove_config"):
        saved = service.repo.get("a", job.id)
        assert saved.status == "CANCELLED" and "private-checkpoint" not in saved.processing_state
        assert processing_view(saved.processing_state)["known_usage"]["video"]["total_tokens"] == 3
    else:
        with pytest.raises(VideoError): service.repo.get("a", job.id)
    assert not service.input_path(job.id).exists()


def test_foreign_workspace_cannot_reuse_or_read_another_jobs_checkpoints(service, media):
    job = setup(service); cloud, asr = clients(); cloud.combine.side_effect = VideoError("failure", 502)
    VideoWorker(service, cloud, asr).run_once()
    with pytest.raises(VideoError): service.repo.get("b", job.id)
    other = setup(service, "b"); next_cloud, next_asr = clients()
    VideoWorker(service, next_cloud, next_asr).run_once()
    assert next_cloud.summarize.call_count == 2 and next_asr.transcribe.call_count == 2
    assert not processing_view(service.repo.get("b", other.id).processing_state)["reused"]


def test_preprocessing_failure_does_not_create_a_cloud_call(service, media, monkeypatch):
    job = setup(service)
    service.put_config("a", {"base_url": DEFAULT_URL, "model": "qwen3-omni-flash", "asr_enabled": False})
    def broken(*_args, **_kwargs): raise VideoError("media failure")
    monkeypatch.setattr("backend.workers.video_worker.make_segment", broken)
    cloud, asr = clients(); VideoWorker(service, cloud, asr).run_once()
    cloud.summarize.assert_not_called()
    assert not processing_view(service.repo.get("a", job.id).processing_state)["calls"]


def test_usage_whitelists_only_finite_nonnegative_provider_values_and_legacy_is_unknown():
    assert reported_usage("summary", {"total_tokens": 4, "prompt_tokens": True, "completion_tokens": float("nan"), "key": "secret"}) == {"total_tokens": 4}
    assert reported_usage("asr", {"duration_seconds": -1, "total_tokens": 100}) == {}
    assert processing_view(None) is None


def test_retrying_precheckpoint_legacy_job_does_not_imply_old_calls_were_free(service, media):
    job = setup(service)
    with service.repo.sessions.begin() as session:
        session.execute(update(VideoJob).where(VideoJob.id == job.id).values(status="FAILED", attempts=1))
    service.retry("a", job.id)
    cloud, asr = clients(); VideoWorker(service, cloud, asr).run_once()
    view = processing_view(service.repo.get("a", job.id).processing_state)
    assert view["untracked_attempts"] == 1 and view["unknown_calls"] == 0
    assert view["known_usage"]["video"]["total_tokens"] == 25


def test_checkpoint_migration_preserves_existing_jobs_and_mysql_longtext(service):
    job = setup(service)
    path = Path(__file__).resolve().parents[2] / "alembic/versions/0014_video_processing_state.py"
    spec = importlib.util.spec_from_file_location("checkpoint_migration", path)
    migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
    engine = service.repo.sessions.kw["bind"]
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            assert "processing_state" not in {c["name"] for c in inspect(connection).get_columns("video_jobs")}
            migration.upgrade()
    assert service.repo.get("a", job.id).processing_state is None
    output = StringIO()
    with Operations.context(MigrationContext.configure(dialect_name="mysql", opts={"as_sql": True, "output_buffer": output})):
        migration.upgrade()
    assert "processing_state LONGTEXT" in output.getvalue()


def test_crash_derived_files_follow_job_deletion_and_old_context_cannot_remove_new_files(service, media):
    job = setup(service); old = service.repo.claim()
    first_context = service.working_directory(old)
    first = first_context.__enter__()
    (first / "segment.mp4").write_bytes(b"private derived fixture")
    with service.repo.sessions.begin() as session:
        session.execute(update(VideoJob).where(VideoJob.id == job.id).values(lease_until=datetime.utcnow() - timedelta(seconds=1)))
    service.repo.claim(); service.retry("a", job.id); new = service.repo.claim()
    with service.working_directory(new) as second:
        (second / "segment.mp4").write_bytes(b"new lease")
        assert first.parent == second.parent == service.directory(job.id)
        assert first != second
        first_context.__exit__(None, None, None)
        assert (second / "segment.mp4").read_bytes() == b"new lease"
        service.delete("a", job.id)
        assert not second.exists()
    assert not service.directory(job.id).exists()
