"""Cross-call ASR boundaries: temporal evidence, readable clauses, stable sources."""
from copy import deepcopy

import pytest

from backend.clients.video_asr import aligned_sentences
from backend.core.video_transcript import append_transcript, same_boundary_word, shift_tail
from backend.tests.test_video_asr import word_response


@pytest.mark.parametrize("text,start,end,expected", [
    (" the", 1999, 2119, True),  # Same word, two slightly different cloud alignments.
    ("THE", 1999, 2119, True),
    ("不", 1999, 2119, False),  # Different meaning, despite identical time range.
    (" the", 2200, 2320, False),  # A real repeated word at a separate time.
    (" the", 2050, 2170, False),  # Small overlap is insufficient evidence.
    (" the", 1000, 3000, False),  # An abnormally long interval cannot erase it.
])
def test_boundary_word_needs_matching_text_and_strong_temporal_evidence(text, start, end, expected):
    assert same_boundary_word({"text": text, "begin_time": start, "end_time": end},
                             {"text": " the", "start_seconds": 1.967, "end_seconds": 2.087}) is expected


def test_duplicate_word_is_removed_before_grouping_with_next_provider_start_preserved():
    data = word_response([{"text": " the", "begin_time": 1999, "end_time": 2119},
                          {"text": " same", "begin_time": 2119, "end_time": 2399},
                          {"text": " page", "begin_time": 2399, "end_time": 2798, "punctuation": "."}])
    rows = aligned_sentences(data, 10, keep_range=(2, 10),
                             previous_tail={"text": "the", "start_seconds": 1.967, "end_seconds": 2.087})
    assert rows[0]["text"] == "same page." and rows[0]["start_seconds"] == 2.119
    assert rows[-1]["_tail_word"] == {"text": " page", "start_seconds": 2.399, "end_seconds": 2.798}
    # Do not mutate provider responses or persisted stages when stitching.
    assert len(data["output"]["sentence"]["words"]) == 3


@pytest.mark.parametrize("punctuation,gap,end,merge", [
    ("", 0, 94, True), ("", -.1, 94, True), ("", .5, 94, True),
    ("。", 0, 94, False), ("，", 0, 94, False),
    ("", .501, 94, False), ("", -.501, 94, False), ("", 0, 110, False),
])
def test_only_short_continuous_unfinished_boundary_clauses_are_joined(punctuation, gap, end, merge):
    sentences = [{"id": 1, "start_seconds": 85, "end_seconds": 90, "text": "现代化离不开农" + punctuation}]
    rows = [{"start_seconds": 2 + gap, "end_seconds": end - 88, "text": "业农村现代化，", "_tail_word": {"private": True}}]
    original = deepcopy(rows)
    append_transcript(sentences, rows, 88)
    assert len(sentences) == (1 if merge else 2)
    assert sentences[-1]["id"] == len(sentences)
    if merge:
        assert sentences[0] == {"id": 1, "start_seconds": 85, "end_seconds": end, "text": "现代化离不开农业农村现代化，"}
    assert rows == original and all("_tail_word" not in row for row in sentences)


def test_english_join_adds_word_space_and_retains_both_endpoints():
    sentences = [{"id": 1, "start_seconds": 86.141, "end_seconds": 90.047, "text": "on the"}]
    append_transcript(sentences, [{"start_seconds": 2.119, "end_seconds": 2.798, "text": "same page."}], 88)
    assert sentences == [{"id": 1, "start_seconds": 86.141, "end_seconds": 90.798, "text": "on the same page."}]
    assert shift_tail({"text": " page", "start_seconds": 2.399, "end_seconds": 2.798}, 88) == {
        "text": " page", "start_seconds": 90.399, "end_seconds": 90.798}
    assert shift_tail(None, 88) is None
