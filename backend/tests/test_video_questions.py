"""Coverage slots and literal evidence guard, independent of cloud agreement."""
import json
from unittest.mock import Mock

import pytest

from backend.clients.video import CloudVideoClient, UNSUPPORTED_ANSWER
from backend.core.video_errors import VideoError
from backend.core.video_questions import question_parts


@pytest.mark.parametrize("query,parts", [
    ("大学是什么？实习医院是哪家？请逐项引用。", ["大学是什么？", "实习医院是哪家？ 请逐项引用。"]),
    ('What does "Are you ready?" mean? Where was it said?', ['What does "Are you ready?" mean?', 'Where was it said?']),
    ("他说“可以经营吗？”是什么意思？", ["他说“可以经营吗？”是什么意思？"]),
    ("请分别回答：\n1. 什么大学\n2. 哪家医院", ["1. 什么大学", "2. 哪家医院"]),
    ("什么？？为什么？", ["什么？？", "为什么？"]),
    ("大学、医院和地点分别是什么？", ["大学、医院和地点分别是什么？"]),
])
def test_explicit_question_boundaries_and_quoted_punctuation(query, parts):
    assert question_parts(query) == parts


def test_over_six_questions_fail_before_any_paid_selection():
    client = CloudVideoClient(); client.generate = Mock()
    with pytest.raises(VideoError) as caught:
        client.answer({"chapters": [{"number": i} for i in range(7)], "overview": "x" * 24001}, "问题？" * 7, [], {})
    assert caught.value.status == 422
    client.generate.assert_not_called()


@pytest.mark.parametrize("draft", [
    '{"answers":{"1":"当前资料无法确认。"}}',
    '{"answers":{"1":"当前资料无法确认。","3":"当前资料无法确认。"}}',
    '{"answers":{"1":"当前资料无法确认。","2":"当前资料无法确认。","3":"当前资料无法确认。"}}',
    '{"answers":{"1":"甲","1":"乙","2":"丙"}}',
])
def test_missing_extra_duplicate_and_wrong_slot_ids_never_release_a_partial_answer(draft):
    client = CloudVideoClient(); client.generate = Mock(return_value=(draft, {}))
    with pytest.raises(VideoError, match="分项回答"):
        client.answer({"chapters": []}, "第一问？第二问？", [], {})
    client.generate.assert_called_once()


def test_slot_order_is_server_owned_even_when_model_reorders_keys():
    client = CloudVideoClient()
    client.generate = Mock(return_value=('{"answers":{"2":"当前资料无法确认。","1":"无法确认。"}}', {}))
    answer, _ = client.answer({"chapters": []}, "第一问？第二问？", [], {})
    assert answer == "1. 无法确认。\n\n2. 当前资料无法确认。"
    payload = json.loads(client.generate.call_args.args[1][0]["text"])
    assert payload["必须逐项回答"] == {"1": "第一问？", "2": "第二问？"}


@pytest.mark.parametrize("draft", [
    "毕业后并未在甲医院工作。[原文 2][原文 3]",
    "毕业后只在乙地工作。[原文 2][原文 3]",
    "He never worked at Hospital A.[原文 2][原文 3]",
    "He worked only in City B.[原文 2][原文 3]",
    "正式文件前不得恢复经营。[原文 2][原文 3]",
])
def test_scoped_claims_render_server_text_even_if_cloud_would_approve_wrong_inference(draft):
    evidence = {"transcript": {"sentences": [
        {"id": 2, "text": "被选派到甲医院实习。"}, {"id": 3, "text": "毕业后在乙地工作。"}]}}
    before = json.dumps(evidence)
    client = CloudVideoClient(); client.generate = Mock(return_value=('should never be trusted', {}))
    answer, usage = client.verify_answer(evidence, "问题含假前提", draft, {})
    assert "被选派到甲医院实习。 [原文 2]" in answer
    assert "毕业后在乙地工作。 [原文 3]" in answer
    assert draft not in answer and "never worked" not in answer and "并未在甲" not in answer
    assert usage == {"extractive_answers": 1} and json.dumps(evidence) == before
    client.generate.assert_not_called()


def test_excerpt_cannot_widen_citations_through_source_text_and_refuses_unbounded_quotes():
    client = CloudVideoClient(); client.generate = Mock()
    evidence = {"transcript": {"sentences": [{"id": 1, "text": "原话[原文 2]"}, {"id": 2, "text": "非本项引用"}]}}
    answer, _ = client.verify_answer(evidence, "q", "只在这里。[原文 1]", {})
    from backend.core.video_citations import citation_ids
    assert citation_ids(answer, "原文", {1, 2}) == {1}
    evidence["transcript"]["sentences"][0]["text"] = "字" * 3501
    assert client.verify_answer(evidence, "q", "只在这里。[原文 1]", {})[0] == UNSUPPORTED_ANSWER
    summary_answer = client.verify_answer({"chapters": [{"number": 1, "summary": "AI未识别"}]}, "q", "从未工作。[片段 1]", {})[0]
    assert "AI分段总结摘录" in summary_answer and "AI未识别 [片段 1]" in summary_answer
    assert "从未工作" not in summary_answer
    client.generate.assert_not_called()


def test_false_premise_routes_to_quotes_without_becoming_part_of_evidence():
    evidence = {"transcript": {"sentences": [{"id": 1, "text": "毕业后在乙地工作。"}]}}
    client = CloudVideoClient(); client.generate = Mock()
    answer, usage = client.verify_answer(evidence, "毕业后在甲地工作，对吗？", "他在甲地工作。[原文 1]", {})
    assert "毕业后在乙地工作。 [原文 1]" in answer and "甲地" not in answer
    assert usage == {"extractive_answers": 1}
    client.generate.assert_not_called()


def test_literal_context_preserves_split_predicate_but_does_not_infer_speaker_or_expand_review():
    from backend.core.video_questions import excerpt_ids
    rows = [{"id": 1, "start_seconds": 10, "end_seconds": 12, "text": "正式文件发布前，网吧、娱乐场所。"},
            {"id": 2, "start_seconds": 13, "end_seconds": 15, "text": "室内游泳馆不得恢复经营。"},
            {"id": 3, "start_seconds": 16, "end_seconds": 18, "text": "只加一条上下文，不继续递归。"},
            {"id": 4, "start_seconds": 25, "end_seconds": 26, "text": "较远原文"}]
    assert excerpt_ids({1}, rows) == {1, 2}
    assert excerpt_ids({4}, rows) == {4}
    client = CloudVideoClient(); client.generate = Mock()
    answer, _ = client.verify_answer({"transcript": {"sentences": rows}}, "可以经营吗？", "可以经营。[原文 1]", {})
    assert "正式文件发布前" in answer and "不得恢复经营" in answer
    assert "[原文 3]" not in answer
    client.generate.assert_not_called()


def test_institution_name_cannot_be_expanded_by_a_draft():
    evidence = {"transcript": {"sentences": [{"id": 1, "text": "在甲医院实习。"}]}}
    client = CloudVideoClient(); client.generate = Mock()
    answer, _ = client.verify_answer(evidence, "在哪家医院实习？", "在乙大学附属甲医院实习。[原文 1]", {})
    assert "在甲医院实习。" in answer and "乙大学" not in answer
    client.generate.assert_not_called()
