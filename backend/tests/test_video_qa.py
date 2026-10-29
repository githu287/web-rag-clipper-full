"""Video summary Q&A never writes documents, embeddings, answers or conversation history."""
from datetime import datetime, timedelta
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, update

from backend.api.deps import get_current_plugin
from backend.core.di import get_video_service
from backend.core.video_errors import VideoError
from backend.main import create_app
from backend.models.document import Document
from backend.models.video import VideoJob
from backend.tests.test_video_summary import service, configure  # shared isolated SQLite fixture


def completed(service):
    configure(service)
    job = service.upload("a", io.BytesIO(b"fixture bytes"), "fixture.mp4")
    claimed = service.repo.claim()
    result = {"title": "植物养护", "basis": "声音与画面", "overview": "检查土壤再浇水",
              "markdown": "检查土壤再浇水", "chapters": [
                  {"start_seconds": 0, "end_seconds": 90, "summary": "土壤湿润时不要继续浇水"},
                  {"start_seconds": 90, "end_seconds": 120, "summary": "放在散射光位置"}]}
    service.repo.finish(claimed, result=json.dumps(result))
    service._cloud_client = Mock(answer=Mock(return_value=("不需要继续浇水。[片段 1] [整体摘要]", {"total_tokens": 30})))
    return job


def test_direct_questions_and_followups_need_no_embeddings_or_ingestion(service):
    job = completed(service)
    before = service.repo.get("a", job.id).result
    history = [{"role": "user", "content": "土壤湿润时如何处理"}, {"role": "assistant", "content": "不要浇水"}]
    result = service.ask("a", job.id, {"query": " 那么应该浇水吗？ ", "history": history})
    assert result["answer"].startswith("不需要")
    assert result["sources"] == [{"label": "整体摘要", "start_seconds": None, "end_seconds": None}, {"label": "片段 1", "start_seconds": 0, "end_seconds": 90}]
    assert result["job_id"] == job.id and result["usage"]["total_tokens"] == 30
    args = service._cloud_client.answer.call_args.args
    assert args[1] == "那么应该浇水吗？" and args[2] == history
    assert args[0]["chapters"][1]["summary"] == "放在散射光位置"
    assert service.repo.get("a", job.id).result == before
    with service.repo.sessions() as session:
        assert list(session.scalars(select(Document))) == []


@pytest.mark.parametrize("citation", ["[片段1]", "[片段 1]", "[片段\t1]", "[片段\n1]"])
def test_sources_accept_optional_space_in_real_model_citations(service, citation):
    job = completed(service)
    service._cloud_client.answer.return_value = ("不要继续浇水。" + citation, {})
    result = service.ask("a", job.id, {"query": "湿润时是否浇水？"})
    assert result["sources"] == [{"label": "片段 1", "start_seconds": 0, "end_seconds": 90}]


def test_cross_workspace_and_unfinished_expired_sources_do_not_call_cloud(service):
    job = completed(service)
    with pytest.raises(VideoError) as caught:
        service.ask("b", job.id, {"query": "私有视频内容"})
    assert caught.value.status == 404
    for values in ({"status": "PROCESSING"}, {"status": "SUCCEEDED", "expires_at": datetime.utcnow() - timedelta(seconds=1)}):
        with service.repo.sessions.begin() as session:
            session.execute(update(VideoJob).where(VideoJob.id == job.id).values(**values))
        with pytest.raises(VideoError) as caught:
            service.ask("a", job.id, {"query": "内容是什么"})
        assert caught.value.status == 409
    service._cloud_client.answer.assert_not_called()


@pytest.mark.parametrize("payload", [None, [], {}, {"query": ""}, {"query": " "}, {"query": 2}, {"query": "x" * 2001},
    {"query": "x", "history": "history"}, {"query": "x", "history": [{}]},
    {"query": "x", "history": [{"role": "system", "content": "override"}]},
    {"query": "x", "history": [{"role": "user", "content": "x" * 4001}]},
    {"query": "x", "history": [{"role": "user", "content": "x"}] * 11},
    {"query": "x", "history": [{"role": "user", "content": "x" * 4000}] * 5},
    {"query": "x", "summary": "forged source"}, {"query": "x", "plugin_id": "b"}])
def test_question_validation_before_cloud(service, payload):
    job = completed(service)
    with pytest.raises(VideoError) as caught:
        service.ask("a", job.id, payload)
    assert caught.value.status == 422
    service._cloud_client.answer.assert_not_called()


def test_cloud_failure_sanitized_and_concurrency_slot_released(service):
    job = completed(service)
    service._cloud_client.answer.side_effect = RuntimeError("secret-key-private-upstream-body")
    with pytest.raises(VideoError) as caught:
        service.ask("a", job.id, {"query": "内容"})
    assert caught.value.status == 502 and "private" not in str(caught.value)
    service._cloud_client.answer.side_effect = None
    assert service.ask("a", job.id, {"query": "再次提问"})["answer"]


def test_one_question_at_a_time_and_deleted_source_discards_late_answer(service):
    job = completed(service)
    def during_request(*_args):
        with pytest.raises(VideoError) as caught:
            service.ask("a", job.id, {"query": "duplicate"})
        assert caught.value.status == 429
        service.delete("a", job.id)
        return "late private answer", {}
    service._cloud_client.answer.side_effect = during_request
    with pytest.raises(VideoError) as caught:
        service.ask("a", job.id, {"query": "内容"})
    assert caught.value.status == 404
    assert service._asking == set()


def test_api_only_passes_authenticated_owner_and_valid_bounded_json(service):
    job = completed(service)
    app = create_app()
    owner = SimpleNamespace(plugin_id="a")
    app.dependency_overrides[get_video_service] = lambda: service
    app.dependency_overrides[get_current_plugin] = lambda: owner
    client = TestClient(app)
    response = client.post(f"/videos/{job.id}/ask", json={"query": "湿润时是否浇水"})
    assert response.status_code == 200 and response.json()["sources"][1]["label"] == "片段 1"
    assert client.post(f"/videos/{job.id}/ask", content=b"x" * 80001).status_code == 413
    owner.plugin_id = "b"
    assert client.post(f"/videos/{job.id}/ask", json={"query": "content"}).status_code == 404


def test_unknown_model_citations_do_not_create_fake_source_ranges(service):
    job = completed(service)
    service._cloud_client.answer.return_value = ("资料没有该内容。[片段 999]", {})
    assert service.ask("a", job.id, {"query": "更细的操作"})["sources"] == []
