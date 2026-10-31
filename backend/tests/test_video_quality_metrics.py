"""Independent subtitle scoring must not hide names, numbers or missing speech."""
import pytest

from scripts.video_quality_metrics import compare_transcript, compare_reference_text, disagreement, read_vtt, tokens


def test_vtt_cue_settings_speaker_markup_and_sound_annotations():
    cues = read_vtt("\ufeffWEBVTT\r\n\r\nNOTE skip\r\nignored\r\n\r\n1\r\n00:01.200 --> 00:03.400 align:start\r\nTEACHER: <b>Don't</b> water &amp; wait. [music]\r\n")
    assert cues == [{"start_seconds": 1.2, "end_seconds": 3.4, "text": "Don't water & wait."}]
    assert tokens("别浇水，Don't! 927") == ["别", "浇", "水", "don't", "927"]


@pytest.mark.parametrize("reference", ["plain transcript", "WEBVTT\n\nNOTE no cues", "WEBVTT\n\n00:04 --> 00:02\nbad", "WEBVTT\n\nNaN:00 --> 00:02\nbad"])
def test_bad_or_empty_reference_rejected_before_paid_calls(reference):
    with pytest.raises(ValueError):
        read_vtt(reference)


def test_edit_distance_keeps_negations_numbers_and_names():
    score = disagreement(tokens("Ana does not use 927"), tokens("Anna does use 928"))
    assert score["edit_distance"] == 3
    assert score["reference_tokens"] == 5
    assert score["token_error_rate"] == .6
    assert disagreement([], ["invented"])["token_error_rate"] is None
    assert disagreement(["speech"], [])["token_error_rate"] == 1


def test_cue_window_does_not_invent_word_alignment_or_score_outside_clip():
    cues = [{"start_seconds": 88, "end_seconds": 92, "text": "do not water"},
            {"start_seconds": 99, "end_seconds": 103, "text": "outside clip"}]
    sentences = [{"start_seconds": 88, "end_seconds": 90, "text": "do not"},
                 {"start_seconds": 90, "end_seconds": 92, "text": "water"}]
    score = compare_transcript(cues, sentences, 100)
    assert score["token_error_rate"] == 0 and score["reference_cues"] == 1
    assert score["boundaries"][0]["cut_seconds"] == 90
    assert score["boundaries"][0]["token_error_rate"] == 0
    with pytest.raises(ValueError):
        compare_transcript(cues, sentences, 80)


def test_normalization_preserves_apostrophes_but_ignores_case_and_punctuation():
    assert tokens("I DON’T wait. A&amp;B") == tokens("i don't wait; a b")


def test_chinese_character_scoring_keeps_negations_names_and_numeric_errors():
    score = compare_reference_text("蔡清华：不得退款８０元。", "蔡清花：得退款90元。")
    assert score["edit_distance"] == 3
    assert score["reference_characters"] == 10
    assert score["character_error_rate"] == .3
    with pytest.raises(ValueError):
        compare_reference_text("...", "捏造")


def test_chinese_scoring_rejects_damaged_reference_and_counts_unknown_asr_characters():
    with pytest.raises(ValueError, match="replacement characters"):
        compare_reference_text("北\ufffd京", "北京")
    score = compare_reference_text("北京", "北\ufffd京")
    assert score["edit_distance"] == 1 and score["character_error_rate"] == .5


def test_damaged_text_reference_stops_paid_runner_before_config_or_cloud(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from unittest.mock import Mock
    from scripts import check_video_quality_live as runner
    manifest = tmp_path / "manifest.json"
    (tmp_path / "reference.txt").write_text("北\ufffd京", encoding="utf-8")
    manifest.write_text(json.dumps([{"file": "fixture.mp4", "reference_text": "reference.txt"}]), encoding="utf-8")
    service = SimpleNamespace(settings=SimpleNamespace(video_segment_seconds=90), config=Mock())
    cloud = Mock()
    monkeypatch.setattr("backend.core.di.get_video_service", lambda: service)
    monkeypatch.setattr("backend.clients.video.CloudVideoClient", cloud)
    monkeypatch.setattr("backend.services.video_media.inspect_video", lambda *_: SimpleNamespace(duration=2, has_audio=True))
    monkeypatch.setattr("sys.argv", ["quality", str(manifest), "--workspace-id", "fixture", "--allow-paid", "--with-asr"])
    with pytest.raises(SystemExit) as caught:
        runner.main()
    assert caught.value.code == 2
    service.config.assert_not_called()
    cloud.assert_not_called()


@pytest.mark.parametrize("reference,actual", [("退款80%", "退款80"), ("参数0.8", "参数08"), ("温度-3度", "温度3度"), ("比例1:2", "比例12")])
def test_chinese_scoring_does_not_erase_meaningful_numeric_symbols(reference, actual):
    assert compare_reference_text(reference, actual)["edit_distance"] == 1
    assert compare_reference_text("８０％", "80%")["edit_distance"] == 0


def test_unique_cue_anchor_checks_real_range_overlap_without_guessing_word_times():
    cues = [{"start_seconds": 10, "end_seconds": 13, "text": "alpha beta gamma delta epsilon"},
            {"start_seconds": 20, "end_seconds": 23, "text": "one two three four five"}]
    rows = [{"start_seconds": 9, "end_seconds": 14, "text": "Well alpha beta gamma delta epsilon."},
            {"start_seconds": 25, "end_seconds": 28, "text": "one two three four five"}]
    alignment = compare_transcript(cues, rows, 30)["cue_alignment"]
    assert alignment["uniquely_matched_cues"] == 2 and alignment["overlapping_ranges"] == 1
    assert alignment["anchors"][0]["asr_start"] == 9


@pytest.mark.parametrize("maximum,exit_code", [(0.1, 0), (True, 2)])
def test_paid_runner_validates_reference_before_cloud_and_reports_scores(tmp_path, monkeypatch, maximum, exit_code):
    import json
    from types import SimpleNamespace
    from unittest.mock import Mock
    from scripts import check_video_quality_live as runner
    output = tmp_path / "report.json"
    manifest = tmp_path / "manifest.json"
    (tmp_path / "reference.vtt").write_text("WEBVTT\n\n00:00 --> 00:01\nPlease do not water now\n", encoding="utf-8")
    manifest.write_text(json.dumps([{"file": "test.mp4", "reference_vtt": "reference.vtt", "max_token_error_rate": maximum,
                                    "questions": [{"query": "What should I do?", "require_asr_citation": True}]}]), encoding="utf-8")
    cloud = Mock()
    cloud.summarize.return_value = ("summary", {"total_tokens": 10})
    cloud.answer.return_value = ("Do not water. [原文 1]", {"total_tokens": 4})
    asr = Mock()
    asr.transcribe.return_value = ([{"start_seconds": 0, "end_seconds": 1, "text": "Please do not water now"}], {"duration_seconds": 2})
    service = SimpleNamespace(settings=SimpleNamespace(video_segment_seconds=90), config=Mock(return_value={"model": "fixture"}))
    monkeypatch.setattr("backend.core.di.get_video_service", lambda: service)
    monkeypatch.setattr("backend.clients.video.CloudVideoClient", lambda: cloud)
    monkeypatch.setattr("backend.clients.video_asr.CloudAsrClient", lambda: asr)
    monkeypatch.setattr("backend.services.video_media.inspect_video", lambda *_: SimpleNamespace(duration=2, has_audio=True))
    monkeypatch.setattr("backend.services.video_media.make_audio", lambda *_: None)
    monkeypatch.setattr("backend.services.video_media.make_segment", lambda *_, **kw: None)
    monkeypatch.setattr("sys.argv", ["quality", str(manifest), "--workspace-id", "fixture", "--with-asr", "--allow-paid", "--output", str(output)])
    with pytest.raises(SystemExit) as caught:
        runner.main()
    assert caught.value.code == exit_code
    if exit_code == 2:
        asr.transcribe.assert_not_called()
        cloud.summarize.assert_not_called()
        service.config.assert_not_called()
    else:
        row = json.loads(output.read_text(encoding="utf-8"))["cases"][0]
        assert row["reference_metrics"]["token_error_rate"] == 0
        assert row["transcript"]["usage"]["duration_seconds"] == 2 and row["passed"]


@pytest.mark.parametrize("count,budget,exit_code", [(4, None, 2), (5, 6, 0), (6, 6, 0), (7, 6, 2), (1, 7, 2)])
def test_paid_runner_extended_chinese_cases_require_explicit_bounded_qa_budget(tmp_path, monkeypatch, count, budget, exit_code):
    import json
    from types import SimpleNamespace
    from unittest.mock import Mock
    from scripts import check_video_quality_live as runner
    output, manifest = tmp_path / "report.json", tmp_path / "manifest.json"
    manifest.write_text(json.dumps([{"file": "fixture.mp4", "questions": [
        {"query": f"独立问题{i}？", "expected": {"required": [["已核对"]]}} for i in range(count)]}]), encoding="utf-8")
    cloud = Mock()
    cloud.summarize.return_value = ("summary", {"total_tokens": 10})
    history_lengths = []
    def answer(_evidence, _query, history, _config):
        history_lengths.append(len(history))
        return "已核对。[片段 1]", {"total_tokens": 4}
    cloud.answer.side_effect = answer
    service = SimpleNamespace(settings=SimpleNamespace(video_segment_seconds=90), config=Mock(return_value={"model": "fixture"}))
    monkeypatch.setattr("backend.core.di.get_video_service", lambda: service)
    monkeypatch.setattr("backend.clients.video.CloudVideoClient", lambda: cloud)
    monkeypatch.setattr("backend.services.video_media.inspect_video", lambda *_: SimpleNamespace(duration=2, has_audio=False))
    monkeypatch.setattr("backend.services.video_media.make_segment", lambda *_, **kw: None)
    args = ["quality", str(manifest), "--workspace-id", "fixture", "--allow-paid", "--output", str(output)]
    if budget is not None:
        args += ["--max-questions", str(budget)]
    monkeypatch.setattr("sys.argv", args)
    with pytest.raises(SystemExit) as caught:
        runner.main()
    assert caught.value.code == exit_code
    if exit_code == 2:
        cloud.summarize.assert_not_called()
        cloud.answer.assert_not_called()
        service.config.assert_not_called()
        assert not output.exists()
    else:
        assert cloud.answer.call_count == count
        assert json.loads(output.read_text(encoding="utf-8"))["passed"]
        # The sixth query has at most the API's ten prior messages.
        assert history_lengths == list(range(0, count * 2, 2))
