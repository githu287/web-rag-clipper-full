"""ASR wire format, timestamp provenance and worker isolation; no paid calls."""
import base64
import io
import json
import subprocess
import wave
from unittest.mock import Mock

import httpx
import pytest

from backend.clients.video_asr import CloudAsrClient, aligned_sentences, ASR_MODEL
from backend.core.video_errors import VideoError
from backend.services.video_media import ffmpeg_executable
from backend.services.video_summary import job_response
from backend.tests.test_video_summary import service, real_video, configure
from backend.workers.video_worker import VideoWorker

CONFIG = {"api_key": "private-fixture-key", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"}


def response_data():
    sentence = {"sentence_end": True, "text": "土壤湿润。不浇水。", "words": [
        {"text": "土壤湿润", "begin_time": 200, "end_time": 1400, "punctuation": "。", "fixed": True},
        {"text": "不浇水", "begin_time": 1600, "end_time": 2500, "punctuation": "。", "fixed": True}]}
    return {"output": {"output": {"sentence": sentence}, "text": sentence["text"]}, "usage": {"duration": 3}}


def test_asr_normalizes_native_nested_result_and_splits_only_using_provider_times():
    assert aligned_sentences(response_data(), 3) == [
        {"start_seconds": .2, "end_seconds": 1.4, "text": "土壤湿润。"},
        {"start_seconds": 1.6, "end_seconds": 2.5, "text": "不浇水。"}]


@pytest.mark.parametrize("change", ["not_final", "missing_words", "unstable", "negative", "outside", "nan", "backwards", "mismatch", "partial"])
def test_incomplete_or_invalid_asr_is_never_presented_as_aligned_original(change):
    data = response_data(); sentence = data["output"]["output"]["sentence"]
    if change == "not_final": sentence["sentence_end"] = False
    if change == "missing_words": sentence["words"] = []
    if change == "unstable": sentence["words"][0]["fixed"] = False
    if change == "negative": sentence["words"][0]["begin_time"] = -1
    if change == "outside": sentence["words"][1]["end_time"] = 5000
    if change == "nan": sentence["words"][0]["begin_time"] = float("nan")
    if change == "backwards": sentence["words"][1]["begin_time"] = 100
    if change == "mismatch": sentence["text"] = "different text"
    if change == "partial": data["output"]["text"] += " missing audio text"
    with pytest.raises(VideoError): aligned_sentences(data, 3)


def test_asr_empty_speech_is_explicit_and_not_hallucinated():
    assert aligned_sentences({"output": {"sentence": {"sentence_end": True, "text": "", "words": []}, "text": ""}}, 2) == []


def test_asr_uses_clause_punctuation_to_avoid_leaving_a_word_as_a_separate_excerpt():
    sentence = {"sentence_end": True, "text": "检查土壤，如果湿润，就不要浇水。", "words": [
        {"text": "检查土壤", "begin_time": 0, "end_time": 4500, "punctuation": "，"},
        {"text": "如果湿润", "begin_time": 5000, "end_time": 6500, "punctuation": "，"},
        {"text": "就不要浇水", "begin_time": 7000, "end_time": 8500, "punctuation": "。"}]}
    rows = aligned_sentences({"output": {"sentence": sentence}}, 9)
    assert [row["text"] for row in rows] == ["检查土壤，", "如果湿润，就不要浇水。"]


def test_native_asr_uses_private_bytes_pinned_model_and_bounded_transport(tmp_path):
    path = tmp_path / "audio.wav"; path.write_bytes(b"test audio")
    requests, options = [], []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=response_data())
    def transport(**kwargs):
        options.append(kwargs)
        return httpx.Client(transport=httpx.MockTransport(handle))
    rows, usage = CloudAsrClient(transport).transcribe(path, 3, CONFIG)
    assert len(rows) == 2 and usage == {"duration_seconds": 3}
    request = requests[0]; payload = json.loads(request.content)
    assert str(request.url) == "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    assert payload["model"] == ASR_MODEL
    assert base64.b64encode(b"test audio").decode() in payload["input"]["messages"][0]["content"][0]["input_audio"]["data"]
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False
    assert "private" not in str(rows)


@pytest.mark.parametrize("status", [302, 400, 401, 403, 429, 500])
def test_asr_error_hides_body_and_does_not_retry(tmp_path, status):
    path = tmp_path / "audio.wav"; path.write_bytes(b"x"); calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, text="private-fixture-key signed-media-url")
    client = CloudAsrClient(lambda **_: httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(VideoError) as caught: client.transcribe(path, 3, CONFIG)
    assert len(calls) == 1 and "private" not in str(caught.value) and "signed-media" not in str(caught.value)


def test_asr_cancelled_before_send_never_contacts_provider(tmp_path):
    path = tmp_path / "audio.wav"; path.write_bytes(b"x")
    factory = Mock()
    with pytest.raises(VideoError) as caught: CloudAsrClient(factory).transcribe(path, 3, CONFIG, lambda: False)
    assert caught.value.status == 409; factory.assert_not_called()


def test_enabled_asr_configuration_is_encrypted_and_old_calls_keep_the_setting(service):
    configure(service)
    assert not service.config("a")["asr_enabled"]
    data = {"base_url": CONFIG["base_url"], "model": "qwen3-omni-flash", "asr_enabled": True}
    assert service.put_config("a", data)["asr_enabled"] is True
    assert service.put_config("a", {"base_url": CONFIG["base_url"], "model": "qwen3-omni-flash"})["asr_enabled"] is True
    for invalid in ("true", 1, None):
        with pytest.raises(VideoError): service.put_config("a", {**data, "asr_enabled": invalid})
    assert "private" not in service.repo.config("a").ciphertext


@pytest.mark.parametrize("explicit_asr", [False, True])
def test_new_key_can_repair_unreadable_video_config(service, explicit_asr):
    service.repo.put_config("a", "corrupt ciphertext", "corrupt nonce")
    payload = {"api_key": "new-private-fixture-key", "base_url": CONFIG["base_url"], "model": "qwen3-omni-flash"}
    if explicit_asr:
        payload["asr_enabled"] = True
    assert service.put_config("a", payload)["configured"]
    assert service.config("a", secret=True)["api_key"] == "new-private-fixture-key"


def test_worker_offsets_transcript_and_keeps_usage_separate(service, real_video):
    configure(service)
    service.put_config("a", {"base_url": CONFIG["base_url"], "model": "qwen3-omni-flash", "asr_enabled": True})
    with real_video.open("rb") as stream: job = service.upload("a", stream, "actual.mp4")
    def transcribe(path, duration, *_args):
        with wave.open(str(path)) as wav:
            assert wav.getframerate() == 16000 and wav.getnchannels() == 1 and wav.getsampwidth() == 2
        return [{"start_seconds": .2, "end_seconds": min(.7, duration), "text": "已识别原文"}], {"duration_seconds": duration}
    asr = Mock(transcribe=Mock(side_effect=transcribe))
    cloud = Mock(); cloud.summarize.return_value = ("summary", {"total_tokens": 10}); cloud.combine.return_value = ("overview", {})
    VideoWorker(service, cloud, asr).run_once()
    result = job_response(service.repo.get("a", job.id))["result"]
    assert result["transcript"]["status"] == "ready"
    assert result["transcript"]["sentences"][1] == {"id": 2, "start_seconds": 10.2, "end_seconds": 10.7, "text": "已识别原文"}
    assert result["usage"]["total_tokens"] == 20 and result["transcript"]["usage"]["duration_seconds"] == 11
    assert not list(service.root.iterdir())


def test_asr_failure_keeps_summary_but_discards_partial_original_and_stops_further_asr_calls(service, real_video):
    configure(service)
    service.put_config("a", {"base_url": CONFIG["base_url"], "model": "qwen3-omni-flash", "asr_enabled": True})
    with real_video.open("rb") as stream: job = service.upload("a", stream, "actual.mp4")
    asr = Mock(transcribe=Mock(side_effect=RuntimeError("private-key")))
    cloud = Mock(); cloud.summarize.return_value = ("summary", {}); cloud.combine.return_value = ("overview", {})
    VideoWorker(service, cloud, asr).run_once()
    response = job_response(service.repo.get("a", job.id))
    assert response["status"] == "SUCCEEDED" and response["result"]["transcript"]["status"] == "failed"
    assert "private" not in json.dumps(response) and response["result"]["transcript"]["sentences"] == []
    asr.transcribe.assert_called_once(); assert cloud.summarize.call_count == 2
    assert not list(service.root.iterdir())


def test_video_without_audio_skips_paid_asr_and_reports_no_audio(service, real_video, tmp_path):
    silent = tmp_path / "silent.mp4"
    subprocess.run([ffmpeg_executable(service.settings), "-nostdin", "-loglevel", "error", "-y",
                    "-i", str(real_video), "-t", "1", "-an", "-c:v", "copy", str(silent)], check=True, timeout=30)
    configure(service)
    service.put_config("a", {"base_url": CONFIG["base_url"], "model": "qwen3-omni-flash", "asr_enabled": True})
    with silent.open("rb") as stream: job = service.upload("a", stream, "silent.mp4")
    asr = Mock(); cloud = Mock(); cloud.summarize.return_value = ("蓝色画面", {})
    VideoWorker(service, cloud, asr).run_once()
    result = job_response(service.repo.get("a", job.id))["result"]
    assert result["transcript"]["status"] == "no_audio" and result["transcript"]["sentences"] == []
    asr.transcribe.assert_not_called()


def test_cancellation_after_transcription_never_starts_video_model_and_cleans_audio(service, real_video):
    configure(service)
    service.put_config("a", {"base_url": CONFIG["base_url"], "model": "qwen3-omni-flash", "asr_enabled": True})
    with real_video.open("rb") as stream: job = service.upload("a", stream, "actual.mp4")
    def cancel(*_args):
        service.cancel("a", job.id)
        return [], {}
    asr = Mock(transcribe=Mock(side_effect=cancel)); cloud = Mock()
    VideoWorker(service, cloud, asr).run_once()
    assert service.repo.get("a", job.id).status == "CANCELLED"
    cloud.summarize.assert_not_called()
    assert not list(service.root.iterdir())
