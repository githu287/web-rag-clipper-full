"""Evidence checks are mandatory gates, not merely additional prompt advice."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.clients.video import CloudVideoClient, UNSUPPORTED_ANSWER
from backend.core.video_errors import VideoError
from backend.services.video_media import inspect_video

CONFIG = {"model": "fixture"}
EVIDENCE = {"overview": "只是AI摘要", "chapters": [{"number": 1, "summary": "不能恢复经营"}],
            "transcript": {"sentences": [{"id": 7, "text": "按80%的比例退款，为19家旅行社共退还170万元。"},
                                         {"id": 12, "text": "正式文件发布前，不得恢复经营。"}]}}


def test_verified_draft_uses_immutable_cited_text_and_counts_extra_cloud_call():
    client = CloudVideoClient()
    client.generate = Mock(return_value=('{"supported":true}', {"total_tokens": 8}))
    draft = "19家旅行社共退还170万元。[原文7]"
    answer, usage = client.verify_answer(EVIDENCE, "退还多少？", draft, CONFIG)
    assert answer == draft and usage == {"total_tokens": 8, "verification_calls": 1}
    payload = json.loads(client.generate.call_args.args[1][0]["text"])
    assert payload["服务端引用原文"] == {"[原文 7]": EVIDENCE["transcript"]["sentences"][0]["text"]}
    assert "只是AI摘要" not in str(payload)
    assert client.generate.call_args.kwargs["max_tokens"] == 128


def test_wrong_negation_or_amount_is_not_published_when_semantic_check_rejects():
    client = CloudVideoClient()
    client.generate = Mock(return_value=('{"supported":false}', {"total_tokens": 9}))
    answer, usage = client.verify_answer(EVIDENCE, "可以恢复经营吗？", "可以恢复经营。[原文12]", CONFIG)
    assert answer == UNSUPPORTED_ANSWER and "[原文" not in answer
    assert usage["verification_rejected"] == 1 and usage["total_tokens"] == 9


@pytest.mark.parametrize("draft", ["没有来源的确定事实", "答案[原文999]", "答案[原文7][原文999]", "答案[原文7-9999]", "答案[原文0]", "答案[原文7、12、14]"])
def test_uncited_unknown_and_unselected_ids_do_not_start_a_verification_call(draft):
    client = CloudVideoClient()
    client.generate = Mock()
    answer, usage = client.verify_answer(EVIDENCE, "事实？", draft, CONFIG)
    assert answer == UNSUPPORTED_ANSWER and usage == {"verification_rejected": 1}
    client.generate.assert_not_called()


@pytest.mark.parametrize("decision", ['{"supported":"true"}', '{"supported":1}', '{}', '[]', 'null', '{"supported":true,"answer":"new claim"}', 'not json'])
def test_malformed_review_cannot_turn_into_an_approved_answer(decision):
    client = CloudVideoClient()
    client.generate = Mock(return_value=(decision, {}))
    with pytest.raises(VideoError) as caught:
        client.verify_answer(EVIDENCE, "事实？", "退款80%。[原文7]", CONFIG)
    assert caught.value.status == 502


def test_question_draft_and_verification_share_deadline(monkeypatch):
    client = CloudVideoClient()
    client.generate = Mock()
    monkeypatch.setattr("backend.clients.video.time.monotonic", lambda: 80)
    with pytest.raises(VideoError) as caught:
        client.verify_answer(EVIDENCE, "事实？", "退款80%。[原文7]", CONFIG, deadline=75)
    assert caught.value.status == 504
    client.generate.assert_not_called()


@pytest.mark.parametrize("seconds,accepted", [("00.00", True), ("00.04", True), ("00.25", True), ("00.26", False), ("01.00", False)])
def test_encoder_padding_only_allows_quarter_second_and_never_extends_processed_timeline(tmp_path, monkeypatch, seconds, accepted):
    source = tmp_path / "encoded.mp4"
    source.write_bytes(b"\x00\x00\x00\x20ftypisom")
    monkeypatch.setattr("backend.services.video_media.ffmpeg_executable", lambda _: "fixture")
    monkeypatch.setattr("backend.services.video_media.run_media", lambda *_, **kw: f"Duration: 00:30:{seconds}, Video: h264 Audio: aac")
    if accepted:
        assert inspect_video(source, SimpleNamespace(video_max_duration_seconds=1800)).duration == 1800
    else:
        with pytest.raises(VideoError):
            inspect_video(source, SimpleNamespace(video_max_duration_seconds=1800))
