"""Exercise the actual OpenAI-compatible SDK/SSE adapter without a paid request."""
import json

import httpx
import pytest

from backend.clients.video import CloudVideoClient
from backend.core.video_errors import VideoError

CONFIG = {"api_key": "fixture-key-private", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen3-omni-flash"}


def test_native_video_and_sse_usage_are_preserved(tmp_path):
    media = tmp_path / "small.mp4"
    media.write_bytes(b"small-private-video")
    requests = []
    def handle(request):
        requests.append(json.loads(request.content))
        events = [{"id": "x", "object": "chat.completion.chunk", "created": 1, "model": CONFIG["model"], "choices": [{"index": 0, "delta": {"content": "视频总结"}, "finish_reason": "stop"}]},
                  {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": CONFIG["model"], "choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7}}]
        content = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text=content)
    transport = httpx.Client(transport=httpx.MockTransport(handle))
    text, usage = CloudVideoClient(transport_factory=lambda **kwargs: transport).summarize(media, CONFIG)
    assert text == "视频总结" and usage["total_tokens"] == 7
    assert requests[0]["stream"] is True and requests[0]["modalities"] == ["text"]
    assert requests[0]["messages"][0]["content"][0]["video_url"]["url"].startswith("data:;base64,")
    assert "private" not in text


@pytest.mark.parametrize("code", [400, 401, 403, 429, 500])
def test_cloud_errors_are_sanitized_and_not_retried(code):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(code, json={"error": {"message": "fixture-key-private signed-media-url secret", "type": "error"}})
    transport = httpx.Client(transport=httpx.MockTransport(handle))
    with pytest.raises(VideoError) as caught:
        CloudVideoClient(transport_factory=lambda **kwargs: transport).combine(["source"], CONFIG)
    assert "private" not in str(caught.value) and "signed-media-url" not in str(caught.value)
    assert len(calls) == 1


def test_incomplete_stream_is_not_accepted_as_a_complete_summary():
    event = {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": CONFIG["model"], "choices": [{"index": 0, "delta": {"content": "partial text"}, "finish_reason": None}]}
    transport = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text="data: " + json.dumps(event) + "\n\n")))
    with pytest.raises(VideoError, match="完整"):
        CloudVideoClient(transport_factory=lambda **kwargs: transport).combine(["source"], CONFIG)


def test_question_prompt_separates_server_evidence_history_and_question():
    from unittest.mock import Mock
    client = CloudVideoClient()
    client.generate = Mock(return_value=("answer", {}))
    evidence = {"overview": "server summary", "chapters": []}
    history = [{"role": "assistant", "content": "untrusted earlier answer"}]
    client.answer(evidence, "follow up?", history, CONFIG)
    args = client.generate.call_args
    prompt = args.args[1][0]["text"]
    assert "不能作为事实证据" in prompt and "禁止声称重新观看了视频" in prompt
    assert '"视频总结资料":' in prompt and '"当前问题": "follow up?"' in prompt
    assert args.kwargs["timeout_seconds"] == 75
