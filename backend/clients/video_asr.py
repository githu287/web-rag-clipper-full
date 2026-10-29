"""Online ASR with provider timestamps; private audio bytes, no public media URLs."""
import base64
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

import httpx

from ..core.video_errors import VideoError

ASR_MODEL = "fun-asr-flash-2026-06-15"


def aligned_sentences(data, duration):
    """Group final ASR words by punctuation; never ask an LLM to invent times."""
    output = data.get("output", {}) if isinstance(data, dict) else {}
    if not isinstance(output, dict):
        raise VideoError("云端转写返回格式不正确", 502)
    nested = output.get("output", output)
    sentence = output.get("sentence") or (nested.get("sentence") if isinstance(nested, dict) else None)
    full_text = output.get("text", "")
    if not isinstance(sentence, dict) or sentence.get("sentence_end") is not True:
        raise VideoError("云端转写未完整返回，不能生成时间定位", 502)
    text = sentence.get("text", "")
    words = sentence.get("words", [])
    if not isinstance(text, str) or not isinstance(words, list) or len(words) > 5000:
        raise VideoError("云端转写内容格式不正确", 502)
    if not text.strip() and not full_text:
        return []
    if not words or len(text) > 12000:
        raise VideoError("转写缺少可靠的字词时间戳", 502)
    result, parts, begin, previous = [], [], None, -1
    def flush(end):
        nonlocal parts, begin
        value = "".join(parts).strip()
        if value:
            result.append({"start_seconds": round(begin / 1000, 3), "end_seconds": round(min(end / 1000, duration), 3), "text": value})
        parts, begin = [], None
    for word in words:
        if not isinstance(word, dict):
            raise VideoError("转写时间戳格式不正确", 502)
        start, end = word.get("begin_time"), word.get("end_time")
        value, punctuation = word.get("text"), word.get("punctuation", "")
        if (isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, (int, float))
            or not isinstance(end, (int, float)) or not math.isfinite(start) or not math.isfinite(end)
            or not 0 <= start < end <= duration * 1000 + 500 or start < previous or start >= duration * 1000
            or not isinstance(value, str) or not isinstance(punctuation, str) or word.get("fixed", True) is not True):
            raise VideoError("转写时间戳不完整或超出素材范围", 502)
        if begin is None:
            begin = start
        parts.append(value + punctuation)
        previous = start
        if (re.search(r"[。！？.!?;；]", punctuation)
            or (re.search(r"[,，:：]", punctuation) and end - begin >= 4000)
            or end - begin >= 20000):
            flush(end)
    if parts:
        flush(end)
    normalize = lambda s: re.sub(r"\s+", "", s)
    joined = "".join(row["text"] for row in result)
    if normalize(joined) != normalize(text) or (isinstance(full_text, str) and full_text and normalize(text) != normalize(full_text)):
        raise VideoError("转写原文与时间戳不完整一致，已停止使用该结果", 502)
    return result


class CloudAsrClient:
    def __init__(self, transport_factory=None):
        self.transport_factory = transport_factory or httpx.Client

    def transcribe(self, path: Path, duration, config, alive=lambda: True):
        if not 0 < path.stat().st_size <= 5 * 1024 * 1024:
            raise VideoError("转写音频分段超过大小限制")
        origin = urlsplit(config["base_url"])
        host = origin.hostname or ""
        if not (host in ("dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com") or
                re.fullmatch(r"[a-zA-Z0-9-]+\.(?:cn-beijing|ap-southeast-1)\.maas\.aliyuncs\.com", host)):
            raise VideoError("云端转写仅支持百炼北京或新加坡地域", 422)
        endpoint = f"https://{host}/api/v1/services/aigc/multimodal-generation/generation"
        started = time.monotonic()
        def check():
            if not alive():
                raise VideoError("任务已取消", 409)
            if time.monotonic() - started > 120:
                raise VideoError("云端转写超时，可稍后手动重试", 504)
        check()
        payload = {"model": ASR_MODEL, "input": {"messages": [{"role": "user", "content": [{"type": "input_audio",
            "input_audio": {"data": "data:audio/wav;base64," + base64.b64encode(path.read_bytes()).decode("ascii")}}]}]},
            "parameters": {"format": "wav", "sample_rate": 16000}}
        try:
            with self.transport_factory(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(30, read=30)) as client:
                with client.stream("POST", endpoint, headers={"Authorization": "Bearer " + config["api_key"],
                        "X-DashScope-SSE": "disable"}, json=payload) as response:
                    check()
                    if response.status_code != 200:
                        raise VideoError("云端转写服务不可用，请检查同地域 Key、Fun-ASR 模型权限或额度", 502)
                    body = bytearray()
                    for block in response.iter_bytes():
                        check()
                        if len(body) + len(block) > 2 * 1024 * 1024:
                            raise VideoError("转写结果超过大小限制", 502)
                        body.extend(block)
            check()
            data = json.loads(body)
            sentences = aligned_sentences(data, duration)
            usage = data.get("usage", {})
            seconds = usage.get("duration", 0) if isinstance(usage, dict) else 0
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
                seconds = 0
            return sentences, {"duration_seconds": seconds}
        except (httpx.HTTPError, ValueError, TypeError):
            raise VideoError("云端转写连接失败或返回无效结果，请稍后重试", 502) from None
