"""Independent video and ASR keys, truthful audio evidence, provider wire contracts."""
import json
from io import BytesIO
from unittest.mock import Mock

import httpx
import pytest

from backend.clients.video import CloudVideoClient
from backend.clients.video_asr import CloudAsrClient
from backend.clients.video_asr_groq import groq_sentences
from backend.core.video_errors import VideoError
from backend.core.video_providers import MODELSCOPE_URL, GROQ_URL, normalize_config, asr_endpoint
from backend.services.video_media import VideoInfo
from backend.tests.test_video_summary import service, configure
from backend.workers.video_worker import VideoWorker


def payload():
    return {"provider": "modelscope", "base_url": MODELSCOPE_URL, "model": "Qwen/Qwen3.5-35B-A3B",
            "api_key": "private-modelscope-key", "asr_enabled": True, "asr_provider": "groq",
            "asr_base_url": GROQ_URL, "asr_model": "whisper-large-v3", "asr_api_key": "private-groq-key"}


def test_separate_keys_encrypted_and_public_configuration_never_returns_either(service):
    configure(service)
    view = service.put_config("a", payload())
    assert view["provider"] == "modelscope" and view["asr_provider"] == "groq" and view["asr_key_configured"]
    assert "private-" not in json.dumps(view)
    assert "api_key" not in view and "asr_api_key" not in view
    row = service.repo.config("a")
    assert "private-" not in row.ciphertext
    secret = service.config("a", secret=True)
    assert secret["api_key"] == "private-modelscope-key"
    assert asr_endpoint(secret)["api_key"] == "private-groq-key"
    next_payload = payload() | {"api_key": "", "asr_api_key": ""}
    service.put_config("a", next_payload)
    assert service.config("a", secret=True) == secret


@pytest.mark.parametrize("change", [
    {"base_url": "https://attacker.example/v1"}, {"asr_base_url": "https://attacker.example/v1"},
    {"asr_api_key": ""}, {"asr_api_key": []}, {"asr_api_key": "bad\nkey-value"},
    {"asr_provider": "custom"}, {"asr_model": "invented"}, {"model": "qwen3-omni-flash"}, {"asr_enabled": False},
])
def test_rejects_unsupported_hosts_keys_models_and_audio_downgrade(change):
    with pytest.raises(VideoError):
        normalize_config(payload() | change, {})


def test_switching_summary_or_asr_provider_cannot_reuse_other_providers_key():
    previous = {"api_key": "private-old-key", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen3-omni-flash"}
    with pytest.raises(VideoError): normalize_config(payload() | {"api_key": ""}, previous)
    previous = normalize_config(payload(), {})
    with pytest.raises(VideoError): normalize_config(payload() | {"asr_provider": "dashscope", "asr_model": "fun-asr-flash-2026-06-15", "asr_base_url": previous["base_url"], "asr_api_key": ""}, previous)


def groq_response():
    return {"text": "土壤湿润，不浇水。 Do not water 80%.", "duration": 5, "words": [
        {"word": "土壤湿润", "start": .2, "end": 1}, {"word": "不浇水", "start": 1.2, "end": 2},
        {"word": "Do", "start": 2.2, "end": 2.5}, {"word": "not", "start": 2.5, "end": 2.8},
        {"word": "water", "start": 2.8, "end": 3.2}, {"word": "80%.", "start": 3.2, "end": 4}]}


def test_groq_preserves_provider_punctuation_word_spaces_negation_and_timestamps():
    rows = groq_sentences(groq_response(), 5)
    assert rows == [{"start_seconds": .2, "end_seconds": 2, "text": "土壤湿润，不浇水。"},
                    {"start_seconds": 2.2, "end_seconds": 4, "text": "Do not water 80%."}]
    assert groq_sentences({"text": "", "words": []}, 5) == []


@pytest.mark.parametrize("damage", ["missing", "nan", "bool", "backwards", "outside", "text_mismatch", "zero"])
def test_groq_invalid_or_partial_word_alignment_is_not_presented_as_original(damage):
    data = groq_response()
    if damage == "missing": data.pop("words")
    if damage == "nan": data["words"][0]["start"] = float("nan")
    if damage == "bool": data["words"][0]["start"] = True
    if damage == "backwards": data["words"][1]["start"] = .1
    if damage == "outside": data["words"][-1]["end"] = 10
    if damage == "text_mismatch": data["text"] = data["text"].replace("不浇水", "浇水")
    if damage == "zero": data["words"][0]["end"] = .2
    with pytest.raises(VideoError): groq_sentences(data, 5)


def test_groq_private_multipart_uses_only_asr_key_and_requests_word_timestamps(tmp_path):
    audio = tmp_path / "private-name.wav"; audio.write_bytes(b"private-audio")
    requests = []
    def send(request):
        requests.append(request)
        assert str(request.url) == GROQ_URL + "/audio/transcriptions"
        assert request.headers["authorization"] == "Bearer private-groq-key"
        body = request.read()
        assert b"private-modelscope-key" not in body and b"private-name" not in body
        assert b'filename="segment.wav"' in body and b'"timestamp_granularities[]"' in body
        assert b"word" in body and b"verbose_json" in body and b"private-audio" in body
        return httpx.Response(200, json=groq_response())
    def factory(**options):
        assert options["follow_redirects"] is False and options["trust_env"] is False
        return httpx.Client(transport=httpx.MockTransport(send), **options)
    rows, usage = CloudAsrClient(factory).transcribe(audio, 5, normalize_config(payload(), {}), keep_range=(0, 5))
    assert rows[0]["text"] == "土壤湿润，不浇水。" and usage == {"duration_seconds": 5}
    assert len(requests) == 1


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_groq_errors_are_sanitized_and_never_automatically_retried(tmp_path, status):
    audio = tmp_path / "audio.wav"; audio.write_bytes(b"audio")
    send = Mock(return_value=httpx.Response(status, json={"error": "private-groq-key"}))
    factory = lambda **options: httpx.Client(transport=httpx.MockTransport(send), **options)
    with pytest.raises(VideoError) as caught:
        CloudAsrClient(factory).transcribe(audio, 5, normalize_config(payload(), {}))
    assert "private-" not in str(caught.value)
    if status == 403: assert "403" in str(caught.value)
    assert send.call_count == 1


def test_groq_known_cancellation_never_sends_audio(tmp_path):
    audio = tmp_path / "audio.wav"; audio.write_bytes(b"audio")
    factory = Mock()
    with pytest.raises(VideoError) as caught:
        CloudAsrClient(factory).transcribe(audio, 5, normalize_config(payload(), {}), alive=lambda: False)
    assert caught.value.status == 409
    factory.assert_not_called()


def test_groq_rejects_oversized_response_before_parsing(tmp_path):
    audio = tmp_path / "audio.wav"; audio.write_bytes(b"audio")
    send = Mock(return_value=httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)))
    factory = lambda **options: httpx.Client(transport=httpx.MockTransport(send), **options)
    with pytest.raises(VideoError, match="上限|限制"):
        CloudAsrClient(factory).transcribe(audio, 5, normalize_config(payload(), {}))
    assert send.call_count == 1


def test_modelscope_prompt_combines_only_explicit_asr_with_visual_input(tmp_path):
    path = tmp_path / "video.mp4"; path.write_bytes(b"video")
    client = CloudVideoClient(); client.generate = Mock(return_value=("summary", {}))
    with pytest.raises(VideoError): client.summarize(path, payload())
    client.generate.assert_not_called()
    speech = [{"id": 1, "text": "不要浇水80%", "start_seconds": .2, "end_seconds": 2}]
    client.summarize(path, payload(), transcript=speech)
    request = client.generate.call_args.args[1]
    assert request[0]["video_url"]["url"].startswith("data:video/mp4;base64,")
    assert "不要浇水80%" in request[1]["text"] and "不得声称直接听取声音" in request[1]["text"]
    assert "private-groq-key" not in json.dumps(request)


@pytest.mark.parametrize("asr_fails", [False, True])
@pytest.mark.parametrize("has_audio", [False, True])
def test_worker_passes_transcript_to_modelscope_and_fails_closed_if_asr_unavailable(service, monkeypatch, asr_fails, has_audio):
    configure(service); service.put_config("a", payload())
    job = service.upload("a", BytesIO(b"immutable source"), "fixture.mp4")
    monkeypatch.setattr("backend.workers.video_worker.inspect_video", lambda *_: VideoInfo(5, has_audio))
    monkeypatch.setattr("backend.workers.video_worker.make_audio", lambda _s, output, *_: output.write_bytes(b"audio"))
    monkeypatch.setattr("backend.workers.video_worker.make_segment", lambda _s, output, *a, **kw: output.write_bytes(b"video"))
    asr, cloud = Mock(), Mock()
    if asr_fails: asr.transcribe.side_effect = VideoError("Groq 服务拒绝访问（403）", 502)
    else: asr.transcribe.return_value = ([{"text": "不要浇水", "start_seconds": .2, "end_seconds": 2}], {"duration_seconds": 5})
    cloud.summarize.return_value = ("ASR讲述不要浇水；画面是蓝色", {"total_tokens": 10})
    VideoWorker(service, cloud, asr).run_once()
    done = service.repo.get("a", job.id)
    if asr_fails and has_audio:
        assert done.status == "FAILED" and done.result is None and "403" in done.error
        cloud.summarize.assert_not_called()
    else:
        result = json.loads(done.result)
        assert result["transcript"]["model"] == "whisper-large-v3"
        assert ("独立云端ASR" if has_audio else "未检测到音轨") in result["basis"]
        assert cloud.summarize.call_args.kwargs["transcript"] == result["transcript"]["sentences"]
        if not has_audio:
            asr.transcribe.assert_not_called()
            assert result["transcript"]["status"] == "no_audio"
        assert done.status == "SUCCEEDED"
