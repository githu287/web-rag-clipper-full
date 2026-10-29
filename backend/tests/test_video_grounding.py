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


def review(*flags, ids=None):
    return json.dumps({"checks": [{"id": i, "reason": "核对说明不应展示", "supported": flag}
                                  for i, flag in zip(ids or range(1, len(flags) + 1), flags)]})


def test_verified_draft_uses_immutable_cited_text_and_counts_extra_cloud_call():
    client = CloudVideoClient()
    client.generate = Mock(return_value=(review(True), {"total_tokens": 8}))
    draft = "19家旅行社共退还170万元。[原文7]"
    answer, usage = client.verify_answer(EVIDENCE, "退还多少？", draft, CONFIG)
    assert answer == draft and usage == {"total_tokens": 8, "verification_calls": 1}
    payload = json.loads(client.generate.call_args.args[1][0]["text"])
    assert payload["分项回答"][0]["服务端引用原文"] == {"[原文 7]": EVIDENCE["transcript"]["sentences"][0]["text"]}
    assert "只是AI摘要" not in str(payload)
    assert client.generate.call_args.kwargs["max_tokens"] == 768


def test_wrong_negation_or_amount_is_not_published_when_semantic_check_rejects():
    client = CloudVideoClient()
    client.generate = Mock(return_value=(review(False), {"total_tokens": 9}))
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


@pytest.mark.parametrize("decision", ['{"checks":"1"}', '{"checks":1}', '{}', '[]', 'null',
    '{"checks":[],"answer":"new claim"}', 'not json', '{"supported":true}',
    review(True, ids=[True]), review(True, ids=["1"]), review(True, ids=[0]),
    review(True, True, ids=[1, 1]), review(True, ids=[2]), review("true"), review(1), review(),
    '{"checks":[{"id":1,"reason":"","supported":true}]}',
    '{"checks":[{"id":1,"reason":true,"supported":true}]}',
    json.dumps({"checks": [{"id": 1, "reason": "x" * 201, "supported": True}]})])
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


def test_partial_answers_keep_only_verified_parts_and_do_not_borrow_other_sources():
    client = CloudVideoClient()
    drafts = ["退款80%。[原文7]", "可以恢复经营。[原文12]"]
    client.generate = Mock(return_value=(review(True, False), {"total_tokens": 9}))
    answer, usage = client.verify_answers(EVIDENCE, "带有假前提的问题", drafts, CONFIG)
    assert answer == "1. " + drafts[0] + "\n\n2. " + UNSUPPORTED_ANSWER
    assert "[原文12]" not in answer and "可以恢复经营" not in answer
    assert usage == {"total_tokens": 9, "verification_calls": 1, "verification_rejected": 1, "verification_partial": 1}
    payload = json.loads(client.generate.call_args.args[1][0]["text"])
    assert set(payload) == {"分项回答"}  # question/history cannot supply missing premises
    assert set(payload["分项回答"][0]["服务端引用原文"]) == {"[原文 7]"}
    assert set(payload["分项回答"][1]["服务端引用原文"]) == {"[原文 12]"}
    client.generate.assert_called_once()


def test_locally_rejected_items_do_not_shift_ids_or_discard_safe_unknowns():
    client = CloudVideoClient()
    client.generate = Mock(return_value=(review(True, ids=[3]), {}))
    drafts = ["虚构[原文999]", "当前资料无法确认。", "退款80%。[原文7]"]
    answer, usage = client.verify_answers(EVIDENCE, "复合问题", drafts, CONFIG)
    assert answer == "1. " + UNSUPPORTED_ANSWER + "\n\n2. 当前资料无法确认。\n\n3. " + drafts[2]
    payload = json.loads(client.generate.call_args.args[1][0]["text"])
    assert [p["id"] for p in payload["分项回答"]] == [3]
    assert usage["verification_partial"] == 1


def test_review_cannot_approve_a_locally_rejected_item():
    client = CloudVideoClient()
    client.generate = Mock(return_value=(review(True, True), {}))
    with pytest.raises(VideoError, match="引用核对"):
        client.verify_answers(EVIDENCE, "复合问题", ["伪造[原文999]", "退款80%。[原文7]"], CONFIG)


def test_review_order_is_resolved_by_id_and_missing_checks_cannot_release_any_draft():
    client = CloudVideoClient()
    drafts = ["退款80%。[原文7]", "可以恢复经营。[原文12]"]
    client.generate = Mock(return_value=(review(False, True, ids=[2, 1]), {}))
    assert client.verify_answers(EVIDENCE, "复合问题", drafts, CONFIG)[0] == "1. " + drafts[0] + "\n\n2. " + UNSUPPORTED_ANSWER
    client.generate.return_value = (review(True), {})
    with pytest.raises(VideoError, match="引用核对"):
        client.verify_answers(EVIDENCE, "复合问题", drafts, CONFIG)


def test_review_failure_does_not_retry_or_release_unchecked_answer():
    client = CloudVideoClient()
    draft = json.dumps({"answers": [{"question": "q", "answer": "退款80%。[原文7]"}]})
    client.generate = Mock(side_effect=[(draft, {}), VideoError("核对服务限流", 502)])
    with pytest.raises(VideoError, match="核对服务限流"):
        client.answer(EVIDENCE, "复合问题", [], CONFIG)
    assert client.generate.call_count == 2


def test_draft_time_reduces_review_budget_instead_of_starting_another_full_deadline(monkeypatch):
    now = {"value": 0}
    monkeypatch.setattr("backend.clients.video.time.monotonic", lambda: now["value"])
    client = CloudVideoClient()
    def generate(*args, **kwargs):
        if now["value"] == 0:
            now["value"] = 62
            return json.dumps({"answers": [{"question": "q", "answer": "退款80%。[原文7]"}]}), {}
        assert kwargs["timeout_seconds"] == 13
        return review(True), {}
    client.generate = Mock(side_effect=generate)
    assert client.answer(EVIDENCE, "问题", [], CONFIG)[0] == "退款80%。[原文7]"
    assert client.generate.call_count == 2


def test_total_rejection_collapses_to_one_refusal_and_never_rewrites():
    client = CloudVideoClient()
    client.generate = Mock(return_value=(review(False, False), {}))
    answer, usage = client.verify_answers(EVIDENCE, "复合问题", ["退款1%。[原文7]", "可以经营。[原文12]"], CONFIG)
    assert answer == UNSUPPORTED_ANSWER and usage["verification_rejected"] == 1
    assert "verification_partial" not in usage
    client.generate.assert_called_once()


def test_unknown_overview_mixed_with_valid_source_is_rejected_locally():
    client = CloudVideoClient()
    client.generate = Mock()
    answer, _ = client.verify_answer({**EVIDENCE, "overview": ""}, "问题", "事实[原文7][整体摘要]", CONFIG)
    assert answer == UNSUPPORTED_ANSWER
    client.generate.assert_not_called()


@pytest.mark.parametrize("draft", ["plain unchecked draft", "null", "[]", "{}", '{"answers":[]}',
    '{"answers":["text"]}', '{"answers":[{"question":"q","answer":true}]}',
    '{"answers":[{"question":"q","answer":""}]}', '{"answers":[{"question":"q","answer":"x","extra":1}]}',
    json.dumps({"answers": [{"question": "q", "answer": "x"}] * 7}),
    json.dumps({"answers": [{"question": "q", "answer": "x" * 2001}]}),
    json.dumps({"answers": [{"question": "q", "answer": "x" * 2000}] * 5})])
def test_malformed_or_oversized_answer_structure_never_starts_review(draft):
    client = CloudVideoClient()
    client.generate = Mock(return_value=(draft, {}))
    with pytest.raises(VideoError, match="分项回答"):
        client.answer(EVIDENCE, "问题", [], CONFIG)
    client.generate.assert_called_once()


def test_answer_to_review_usage_and_order_include_only_reviewed_text():
    client = CloudVideoClient()
    draft = {"answers": [{"question": "不要泄漏这个规划问题", "answer": "退款80%。[原文7]"},
                         {"question": "另一个问题", "answer": "可以恢复经营。[原文12]"}]}
    client.generate = Mock(side_effect=[(json.dumps(draft), {"total_tokens": 20}), (review(True, False), {"total_tokens": 5})])
    answer, usage = client.answer(EVIDENCE, "复合问题", [], CONFIG)
    assert answer.startswith("1. 退款80%。[原文7]") and answer.endswith(UNSUPPORTED_ANSWER)
    assert "规划问题" not in answer and "规划问题" not in client.generate.call_args.args[1][0]["text"]
    assert usage["total_tokens"] == 25 and usage["verification_partial"] == 1
    assert "核对说明" not in answer
    assert client.generate.call_count == 2


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
