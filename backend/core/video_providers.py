"""Allowlisted video/ASR endpoints and independent encrypted credentials."""
import re

from .video_errors import VideoError

DEFAULT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
MODELSCOPE_URL = "https://api-inference.modelscope.cn/v1"
GROQ_URL = "https://api.groq.com/openai/v1"
ASR_MODEL = "fun-asr-flash-2026-06-15"
MODELS = ("qwen3-omni-flash", "qwen3.5-omni-flash", "qwen3.5-omni-plus", "qwen3.8-omni-flash")
GROQ_MODELS = ("whisper-large-v3", "whisper-large-v3-turbo")
VIDEO_PROVIDERS = {
    "dashscope": {"label": "阿里云百炼（音视频）", "base_url": DEFAULT_URL, "models": MODELS},
    "modelscope": {"label": "魔搭社区（画面 + 独立语音）", "base_url": MODELSCOPE_URL,
                  "models": ("Qwen/Qwen3.5-35B-A3B",)},
}
ASR_PROVIDERS = {
    "dashscope": {"label": "百炼 Fun-ASR", "base_url": DEFAULT_URL, "models": (ASR_MODEL,)},
    "groq": {"label": "Groq Whisper", "base_url": GROQ_URL, "models": GROQ_MODELS},
}


def is_modelscope(config):
    return config.get("base_url", "").rstrip("/") == MODELSCOPE_URL


def dashscope_url(value, *, asr=False):
    if not isinstance(value, str) or not re.fullmatch(
        r"https://(?:(?:dashscope(?:-intl|-us)?\.aliyuncs\.com)|(?:[a-zA-Z0-9-]+\.(?:cn-beijing|cn-shanghai|ap-southeast-1|us-east-1)\.maas\.aliyuncs\.com))/compatible-mode/v1/?", value):
        raise VideoError("请填写百炼控制台提供的 HTTPS Chat Completions 服务地址（以 /compatible-mode/v1 结尾）", 422)
    if asr and any(part in value for part in ("-us.aliyuncs.com", "us-east-1", "cn-shanghai")):
        raise VideoError("云端原文转写请使用百炼北京或新加坡地域的地址与 Key", 422)
    return value.rstrip("/")


def checked_key(key, label="API Key"):
    if not isinstance(key, str) or not 10 <= len(key.strip()) <= 512 or any(ord(ch) < 32 for ch in key):
        raise VideoError(label + " 格式不正确", 422)
    return key.strip()


def normalize_config(payload, previous):
    allowed = {"provider", "api_key", "base_url", "model", "asr_enabled", "asr_provider", "asr_api_key", "asr_base_url", "asr_model"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise VideoError("视频配置字段不正确", 422)
    url, model = payload.get("base_url"), payload.get("model")
    provider = payload.get("provider", "modelscope" if isinstance(url, str) and url.rstrip("/") == MODELSCOPE_URL else "dashscope")
    if not isinstance(provider, str) or provider not in VIDEO_PROVIDERS:
        raise VideoError("视频服务商不受支持", 422)
    if provider == "modelscope":
        if not isinstance(url, str) or url.rstrip("/") != MODELSCOPE_URL:
            raise VideoError("魔搭服务地址必须为 https://api-inference.modelscope.cn/v1", 422)
        url = MODELSCOPE_URL
    else:
        url = dashscope_url(url)
    if not isinstance(model, str) or model not in VIDEO_PROVIDERS[provider]["models"]:
        raise VideoError("请选择该服务商支持的视频模型", 422)
    key = payload.get("api_key")
    old_provider = "modelscope" if is_modelscope(previous) else "dashscope"
    if key in (None, ""):
        if provider != old_provider or not previous.get("api_key"):
            raise VideoError("切换服务商时请填写对应的新 API Key", 422)
        key = previous["api_key"]
    key = checked_key(key)
    enabled = payload.get("asr_enabled", previous.get("asr_enabled", provider == "modelscope"))
    if not isinstance(enabled, bool):
        raise VideoError("云端原文转写开关格式不正确", 422)
    if provider == "modelscope" and not enabled:
        raise VideoError("魔搭画面模型需开启独立语音转写，才能保留视频声音内容", 422)
    asr_provider = payload.get("asr_provider", previous.get("asr_provider", "groq" if provider == "modelscope" else "dashscope"))
    if not isinstance(asr_provider, str) or asr_provider not in ASR_PROVIDERS:
        raise VideoError("转写服务商不受支持", 422)
    same_asr = asr_provider == previous.get("asr_provider", "dashscope")
    asr_key = payload.get("asr_api_key")
    if asr_key in (None, ""):
        asr_key = previous.get("asr_api_key", "") if same_asr else ""
    if asr_key != "":
        asr_key = checked_key(asr_key, "转写 API Key")
    elif enabled and (asr_provider == "groq" or provider != "dashscope"):
        raise VideoError("请填写独立语音服务的 API Key；视频服务的 Key 不会发送给其他服务商", 422)
    if asr_provider == "groq":
        asr_url = payload.get("asr_base_url", GROQ_URL)
        if not isinstance(asr_url, str) or asr_url.rstrip("/") != GROQ_URL:
            raise VideoError("Groq 转写地址必须为 https://api.groq.com/openai/v1", 422)
        asr_url = GROQ_URL
    else:
        default_asr_url = previous.get("asr_base_url", DEFAULT_URL) if same_asr else DEFAULT_URL
        if provider == "dashscope" and not asr_key:
            default_asr_url = url
        asr_url = dashscope_url(payload.get("asr_base_url", default_asr_url), asr=enabled)
    asr_model = payload.get("asr_model", previous.get("asr_model") if same_asr else None) or ASR_PROVIDERS[asr_provider]["models"][0]
    if not isinstance(asr_model, str) or asr_model not in ASR_PROVIDERS[asr_provider]["models"]:
        raise VideoError("请选择该转写服务商支持的模型", 422)
    return {"provider": provider, "base_url": url, "model": model, "api_key": key, "asr_enabled": enabled,
            "asr_provider": asr_provider, "asr_base_url": asr_url, "asr_model": asr_model, "asr_api_key": asr_key}


def asr_endpoint(config):
    provider = config.get("asr_provider", "dashscope")
    key = config.get("asr_api_key") or (config.get("api_key") if not is_modelscope(config) and provider == "dashscope" else None)
    if not key:
        raise VideoError("独立语音服务缺少 API Key，请重新保存视频配置", 409)
    return {"provider": provider, "api_key": key, "base_url": config.get("asr_base_url", config["base_url"]),
            "model": config.get("asr_model", ASR_MODEL)}


def public_config(data, configured=True):
    provider = "modelscope" if is_modelscope(data) else "dashscope"
    asr_provider = data.get("asr_provider", "dashscope")
    return {"configured": configured, "provider": provider, "base_url": data["base_url"], "model": data["model"],
            "models": VIDEO_PROVIDERS[provider]["models"], "providers": VIDEO_PROVIDERS, "asr_providers": ASR_PROVIDERS,
            "asr_enabled": data.get("asr_enabled", False), "asr_provider": asr_provider,
            "asr_base_url": data.get("asr_base_url", data["base_url"]),
            "asr_model": data.get("asr_model", ASR_MODEL), "asr_key_configured": bool(data.get("asr_api_key"))}
