"""Workspace 级 OpenAI 兼容模型服务配置。

Embedding 与 LLM 可以使用不同服务商和 API Key。明文配置只存在于请求调用栈；
持久化时由 PluginService 将完整 JSON 使用 AES-256-GCM 加密。
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Final
from urllib.parse import urlparse


MODEL_CONFIG_VERSION: Final[int] = 1
EMBEDDING_DIMENSION: Final[int] = 1024


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    provider_id: str
    label: str
    base_url: str
    default_model: str
    send_dimensions: bool = True


EMBEDDING_PROVIDER_PRESETS: Final[dict[str, ProviderPreset]] = {
    "dashscope": ProviderPreset(
        "dashscope", "阿里云百炼", "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "text-embedding-v3", True,
    ),
    "openai": ProviderPreset(
        "openai", "OpenAI", "https://api.openai.com/v1",
        "text-embedding-3-small", True,
    ),
    "gemini": ProviderPreset(
        "gemini",
        "Google Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "gemini-embedding-001", True,
    ),
    "siliconflow": ProviderPreset(
        "siliconflow", "硅基流动", "https://api.siliconflow.cn/v1",
        "BAAI/bge-m3", False,
    ),
}

LLM_PROVIDER_PRESETS: Final[dict[str, ProviderPreset]] = {
    "dashscope": ProviderPreset(
        "dashscope", "阿里云百炼", "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen-plus", False,
    ),
    "openai": ProviderPreset(
        "openai", "OpenAI", "https://api.openai.com/v1",
        "gpt-4.1-mini", False,
    ),
    "gemini": ProviderPreset(
        "gemini",
        "Google Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "gemini-2.5-flash", False,
    ),
    "deepseek": ProviderPreset(
        "deepseek", "DeepSeek", "https://api.deepseek.com/v1",
        "deepseek-chat", False,
    ),
    "siliconflow": ProviderPreset(
        "siliconflow", "硅基流动", "https://api.siliconflow.cn/v1",
        "Qwen/Qwen3-8B", False,
    ),
    "openrouter": ProviderPreset(
        "openrouter", "OpenRouter", "https://openrouter.ai/api/v1",
        "openai/gpt-4.1-mini", False,
    ),
}


@dataclass(frozen=True, slots=True)
class ModelEndpointCredential:
    provider: str
    api_key: str
    base_url: str
    model: str
    send_dimensions: bool = False

    def safe_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "send_dimensions": self.send_dimensions,
            "api_key_configured": bool(self.api_key),
        }


class WorkspaceModelCredentials(str):
    """兼容旧 ``api_key: str`` 调用链的双端点凭证。

    字符串值等于 Embedding Key，因此既有 Service 只透传、不检查内容时无需改动；
    EmbeddingClient/LLMClient 会读取各自对应的 endpoint 属性。
    """

    embedding: ModelEndpointCredential
    llm: ModelEndpointCredential

    def __new__(
        cls,
        embedding: ModelEndpointCredential,
        llm: ModelEndpointCredential,
    ) -> "WorkspaceModelCredentials":
        obj = str.__new__(cls, embedding.api_key)
        obj.embedding = embedding
        obj.llm = llm
        return obj

    def to_encrypted_payload(self) -> dict[str, object]:
        def endpoint_dict(endpoint: ModelEndpointCredential) -> dict[str, object]:
            return {
                "provider": endpoint.provider,
                "api_key": endpoint.api_key,
                "base_url": endpoint.base_url,
                "model": endpoint.model,
                "send_dimensions": endpoint.send_dimensions,
            }

        return {
            "version": MODEL_CONFIG_VERSION,
            "embedding": endpoint_dict(self.embedding),
            "llm": endpoint_dict(self.llm),
        }


def normalize_base_url(value: str) -> str:
    normalized = (value or "").strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("base_url 必须是未携带凭据的 HTTPS 地址")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url 不允许包含 query 或 fragment")
    return normalized


def build_endpoint(
    *,
    kind: str,
    provider: str,
    api_key: str,
    model: str,
    base_url: str | None = None,
    send_dimensions: bool | None = None,
) -> ModelEndpointCredential:
    provider_id = (provider or "").strip().lower()
    presets = (
        EMBEDDING_PROVIDER_PRESETS
        if kind == "embedding"
        else LLM_PROVIDER_PRESETS
    )
    preset = presets.get(provider_id)
    if provider_id != "custom" and preset is None:
        raise ValueError(f"不支持的 {kind} 服务商：{provider_id or '空'}")
    key = (api_key or "").strip()
    selected_model = (model or "").strip()
    if not key:
        raise ValueError(f"{kind} API Key 不能为空")
    if not selected_model:
        raise ValueError(f"{kind} 模型不能为空")
    if len(key) > 1024 or len(selected_model) > 128:
        raise ValueError(f"{kind} 配置长度超出限制")
    if preset is not None:
        endpoint_url = preset.base_url
        dimensions_flag = (
            preset.send_dimensions if send_dimensions is None else send_dimensions
        )
    else:
        endpoint_url = normalize_base_url(base_url or "")
        dimensions_flag = bool(send_dimensions)
    return ModelEndpointCredential(
        provider=provider_id,
        api_key=key,
        base_url=endpoint_url,
        model=selected_model,
        send_dimensions=bool(dimensions_flag) if kind == "embedding" else False,
    )


def legacy_dashscope_credentials(api_key: str) -> WorkspaceModelCredentials:
    return WorkspaceModelCredentials(
        build_endpoint(
            kind="embedding",
            provider="dashscope",
            api_key=api_key,
            model=EMBEDDING_PROVIDER_PRESETS["dashscope"].default_model,
        ),
        build_endpoint(
            kind="llm",
            provider="dashscope",
            api_key=api_key,
            model=LLM_PROVIDER_PRESETS["dashscope"].default_model,
        ),
    )


def embedding_config_fingerprint(endpoint: ModelEndpointCredential) -> str:
    """不含 Key 的 embedding space 指纹，用于阻止混用不同向量空间。"""
    value = "\0".join(
        (
            endpoint.provider,
            endpoint.base_url,
            endpoint.model,
            "1" if endpoint.send_dimensions else "0",
            str(EMBEDDING_DIMENSION),
        )
    )
    return sha256(value.encode("utf-8")).hexdigest()


def credentials_from_payload(payload: dict[str, object]) -> WorkspaceModelCredentials:
    if payload.get("version") != MODEL_CONFIG_VERSION:
        raise ValueError("不支持的模型配置版本")

    def parse(kind: str) -> ModelEndpointCredential:
        raw = payload.get(kind)
        if not isinstance(raw, dict):
            raise ValueError(f"缺少 {kind} 配置")
        return build_endpoint(
            kind=kind,
            provider=str(raw.get("provider") or ""),
            api_key=str(raw.get("api_key") or ""),
            model=str(raw.get("model") or ""),
            base_url=str(raw.get("base_url") or ""),
            send_dimensions=bool(raw.get("send_dimensions", False)),
        )

    return WorkspaceModelCredentials(parse("embedding"), parse("llm"))


def provider_catalog() -> dict[str, list[dict[str, object]]]:
    def serialize(preset: ProviderPreset) -> dict[str, object]:
        return {
            "id": preset.provider_id,
            "label": preset.label,
            "base_url": preset.base_url,
            "default_model": preset.default_model,
            "send_dimensions": preset.send_dimensions,
        }

    embedding = [serialize(item) for item in EMBEDDING_PROVIDER_PRESETS.values()]
    llm = [serialize(item) for item in LLM_PROVIDER_PRESETS.values()]
    custom = {
        "id": "custom",
        "label": "自定义 OpenAI 兼容服务",
        "base_url": "",
        "default_model": "",
        "send_dimensions": False,
    }
    return {"embedding": [*embedding, custom], "llm": [*llm, custom]}
