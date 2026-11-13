"""Groq Whisper word timestamps via private multipart audio, no local inference."""
import json
import math
import time
import unicodedata

import httpx

from ..core.video_errors import VideoError
from ..core.video_providers import GROQ_URL, GROQ_MODELS
from .video_transport import DeadlineTransport


def groq_sentences(data, duration, **options):
    from .video_asr import aligned_sentences
    if not isinstance(data, dict) or not isinstance(data.get("text"), str) or not isinstance(data.get("words"), list):
        raise VideoError("Groq 转写缺少完整原文或字词时间戳", 502)
    text, raw = data["text"], data["words"]
    if len(text) > 12000 or len(raw) > 5000:
        raise VideoError("Groq 转写内容超过上限", 502)
    words, cursor = [], 0
    def nonverbal(value):
        return all(char.isspace() or unicodedata.category(char).startswith("P") for char in value)
    for word in raw:
        if not isinstance(word, dict) or not isinstance(word.get("word"), str) or not word["word"].strip():
            raise VideoError("Groq 转写字词格式不正确", 502)
        value = word["word"].strip()
        start, end = word.get("start"), word.get("end")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (start, end)):
            raise VideoError("Groq 转写字词时间不完整", 502)
        position = text.find(value, cursor)
        if position < 0 or not nonverbal(text[cursor:position]):
            raise VideoError("Groq 原文与字词时间戳不一致", 502)
        gap = text[cursor:position]
        if words:
            words[-1]["punctuation"] += gap
        else:
            value = gap + value
        cursor = position + len(word["word"].strip())
        # Keep punctuation from the provider's own text, never generate it.
        split = len(value)
        while split and nonverbal(value[split - 1]):
            split -= 1
        words.append({"text": value[:split], "punctuation": value[split:], "begin_time": start * 1000, "end_time": end * 1000})
    if not nonverbal(text[cursor:]) or (text.strip() and not words):
        raise VideoError("Groq 转写原文未完整对齐", 502)
    if words:
        words[-1]["punctuation"] += text[cursor:]
    wire = {"output": {"text": text, "sentence": {"sentence_end": True, "text": text, "words": words}}}
    return aligned_sentences(wire, duration, **options)


class GroqAsrClient:
    def __init__(self, transport_factory=None):
        self.transport_factory = transport_factory

    def transcribe(self, path, duration, config, alive=lambda: True, **options):
        if config["base_url"] != GROQ_URL or config["model"] not in GROQ_MODELS:
            raise VideoError("Groq 转写服务地址或模型不受支持", 422)
        if not 0 < path.stat().st_size <= 5 * 1024 * 1024:
            raise VideoError("转写音频分段超过大小限制", 422)
        started = time.monotonic()
        def check():
            if not alive():
                raise VideoError("任务已取消", 409)
            if time.monotonic() - started >= 120:
                raise VideoError("Groq 转写超时，可稍后手动重试", 504)
        def remaining():
            check()
            return max(120 - (time.monotonic() - started), .000001)
        check()
        factory = self.transport_factory or httpx.Client
        settings = {"trust_env": False, "follow_redirects": False, "timeout": httpx.Timeout(30, read=30)}
        if self.transport_factory is None:
            settings["transport"] = DeadlineTransport(remaining)
        try:
            with factory(**settings) as client, path.open("rb") as audio:
                with client.stream("POST", GROQ_URL + "/audio/transcriptions",
                        headers={"Authorization": "Bearer " + config["api_key"]},
                        files={"file": ("segment.wav", audio, "audio/wav")},
                        data={"model": config["model"], "response_format": "verbose_json", "temperature": "0",
                              "timestamp_granularities[]": ["word", "segment"]}) as response:
                    check()
                    if response.status_code != 200:
                        if response.status_code == 403:
                            message = "Groq 转写服务拒绝访问（403），请检查账户权限与服务访问条件"
                        elif response.status_code == 401:
                            message = "Groq 转写认证失败，请检查独立 ASR API Key"
                        elif response.status_code == 429:
                            message = "Groq 转写限流或额度不足，请检查控制台后手动重试"
                        else:
                            message = "Groq 转写服务不可用，请稍后手动重试"
                        raise VideoError(message, 502)
                    body = bytearray()
                    for block in response.iter_bytes():
                        check()
                        if len(body) + len(block) > 2 * 1024 * 1024:
                            raise VideoError("转写结果超过大小限制", 502)
                        body.extend(block)
            check()
            data = json.loads(body)
            rows = groq_sentences(data, duration, **options)
            seconds = data.get("duration", 0)
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
                seconds = 0
            return rows, {"duration_seconds": seconds}
        except (httpx.HTTPError, ValueError, TypeError):
            check()
            raise VideoError("Groq 转写连接失败或返回无效结果，请稍后重试", 502) from None
