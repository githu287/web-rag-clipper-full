"""ASR display context must preserve scope, boundaries and original references."""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from backend.clients.video import CloudVideoClient
from backend.core.video_evidence import continuous_transcript


def rows():
    return [{"id": 228, "start_seconds": 1076, "end_seconds": 1081, "text": "在正式文件发布前，网吧、娱乐场所，"},
            {"id": 229, "start_seconds": 1081.3, "end_seconds": 1085, "text": "室内游泳馆都不得恢复经营。"},
            {"id": 230, "start_seconds": 1088, "end_seconds": 1090, "text": "下一个话题。"}]


def test_adjacent_fragments_read_as_one_passage_but_keep_their_original_ids_and_text():
    original = rows()
    before = deepcopy(original)
    assert continuous_transcript(original) == [{"sources": ["[原文 228]", "[原文 229]"],
                                                "text": original[0]["text"] + " " + original[1]["text"]}]
    assert original == before


@pytest.mark.parametrize("ids", [{229}, {228}, {228, 230}, set()])
def test_review_context_never_supplies_an_uncited_subject_or_condition(ids):
    assert continuous_transcript(rows(), ids) == []


@pytest.mark.parametrize("change", [
    {"id": 230}, {"start_seconds": 1083.01}, {"start_seconds": 1080},
    {"start_seconds": float('nan')}, {"end_seconds": float('inf')},
    {"start_seconds": True}, {"end_seconds": 1081.3}, {"end_seconds": 1107},
    {"text": "x" * 1001}, {"start_seconds": None},
])
def test_gaps_overlap_missing_times_and_large_passages_do_not_merge(change):
    source = rows()[:2]
    source[1].update(change)
    assert continuous_transcript(source) == []


@pytest.mark.parametrize("ending", ["。", "！", "？", ".", "!", "?", "。”", '?"'])
def test_asr_punctuation_is_preserved_in_context_not_silently_corrected(ending):
    source = rows()[:2]
    source[0]["text"] += ending
    assert continuous_transcript(source)[0]["text"] == source[0]["text"] + " " + source[1]["text"]


def test_long_run_has_bounded_groups_and_no_overlapping_or_new_sources():
    source = [{"id": i, "text": "fragment,", "start_seconds": i, "end_seconds": i + .8} for i in range(1, 10)]
    groups = continuous_transcript(source)
    assert [len(g["sources"]) for g in groups] == [4, 4]
    assert len({s for g in groups for s in g["sources"]}) == 8


def test_additional_context_is_bounded_for_a_large_transcript():
    source = [{"id": i, "text": "字" * 200, "start_seconds": i, "end_seconds": i + .8} for i in range(1, 100)]
    groups = continuous_transcript(source)
    assert sum(len(json.dumps(g, ensure_ascii=False)) for g in groups) <= 6000
    assert groups and all(len(g["sources"]) <= 4 for g in groups)


def test_review_adds_continuous_context_only_for_explicitly_cited_rows_of_that_item():
    evidence = {"transcript": {"sentences": rows()}, "chapters": []}
    client = CloudVideoClient()
    client.generate = Mock(return_value=(json.dumps({"checks": [
        {"id": 1, "reason": "full citation", "supported": True},
        {"id": 2, "reason": "missing premise", "supported": False}]}), {}))
    client.verify_answers(evidence, "question", ["事实[原文228][原文229]", "事实[原文229]"], {})
    candidates = json.loads(client.generate.call_args.args[1][0]["text"])["分项回答"]
    assert candidates[0]["服务端引用原文"] == {"[原文 228][原文 229]": "[原文 228] " + rows()[0]["text"] + " [原文 229] " + rows()[1]["text"]}
    assert candidates[1]["服务端引用原文"] == {"[原文 229]": rows()[1]["text"]}
    assert "在正式文件" not in str(candidates[1])


def test_review_preserves_source_boundaries_when_the_joined_words_are_identical():
    client = CloudVideoClient()
    client.generate = Mock(return_value=(json.dumps({"checks": [
        {"id": 1, "reason": "fixture", "supported": False}]}), {}))
    payloads = []
    for texts in (("Opening.", "Revenue 20. Closing."), ("Opening. Revenue 20.", "Closing.")):
        evidence = {"transcript": {"sentences": [
            {"id": i + 1, "text": text, "start_seconds": i, "end_seconds": i + .8}
            for i, text in enumerate(texts)]}}
        client.verify_answers(evidence, "question", ["Revenue is 20.[原文1] Closing.[原文2]"], {})
        payloads.append(json.loads(client.generate.call_args.args[1][0]["text"]))
    assert payloads[0] != payloads[1]
    assert payloads[0]["分项回答"][0]["服务端引用原文"] == {
        "[原文 1][原文 2]": "[原文 1] Opening. [原文 2] Revenue 20. Closing."}
    assert payloads[1]["分项回答"][0]["服务端引用原文"] == {
        "[原文 1][原文 2]": "[原文 1] Opening. Revenue 20. [原文 2] Closing."}


def test_generation_context_contains_exact_sources_and_does_not_mutate_stored_transcript():
    evidence = {"transcript": {"sentences": rows()}, "chapters": []}
    before = deepcopy(evidence)
    client = CloudVideoClient()
    client.generate = Mock(return_value=('{"answers":[{"question":"q","answer":"当前资料无法确认。"}]}', {}))
    client.answer(evidence, "question", [], {})
    transcript = json.loads(client.generate.call_args.args[1][0]["text"])["视频总结资料"]["transcript"]
    assert transcript["连续转写"] == continuous_transcript(rows())
    assert evidence == before
