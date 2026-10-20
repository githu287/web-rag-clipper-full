"""
百炼 LLM Chat Completion API 客户端封装（Phase 3.3 Step 3）。

职责：
    (system_prompt, user_prompt) → 百炼 OpenAI 兼容 Chat API → 回答文本

严格不负责：
    - RAG 检索 / Context 构造 / Prompt 模板（Service 层职责）
    - 业务状态管理

设计要点（与 clients/embedding.py 风格对齐）：
1) 配置单源：API Key / base_url / model 全部从 Settings 注入，禁止硬编码。
2) 延迟真实连接：__init__ 仅保存配置，不创建 OpenAI client；generate() 调用时才惰性创建。
   保证「无 .env / 无网络」也能 import 本模块、能构造对象（便于单元测试与启动期装配）。
3) 异常不吞：openai SDK 抛出的所有异常统一包装为 LLMClientError 族并保留 __cause__。
4) 空响应防御：choices[0].message.content 为 None / 空字符串 → LLMClientEmptyResponseError
   （防止空 answer 进入 API 响应）。
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Protocol

from openai import OpenAI

from ..core.config import Settings
from ..core.security import sha256_hex
from ..models.model_provider import ModelEndpointCredential, WorkspaceModelCredentials

logger: logging.Logger = logging.getLogger(__name__)
_MAX_CLIENT_CACHE_SIZE = 32


# --------------------------------------------------------------------------- 异常定义
class LLMClientError(Exception):
    """LLM 客户端异常根类（与 clients.embedding.EmbeddingClientError 平级的外部客户端异常族）。"""


class LLMClientConfigError(LLMClientError):
    """配置类错误（API Key 为空 / base_url 非法 / model 未设置等），不可重试。"""


class LLMClientRequestError(LLMClientError):
    """LLM API 调用类错误（网络 / 超时 / 限流 / 5xx），可由上层决定重试。"""


class LLMClientResponseError(LLMClientError):
    """LLM 返回契约不一致（choices 缺失 / message 缺失 / content 字段异常），不可重试。"""


class LLMClientEmptyResponseError(LLMClientError):
    """LLM 返回空响应（content 为 None 或 strip 后为空字符串），不可重试。"""


# --------------------------------------------------------------------------- Protocol
class LLMClient(Protocol):
    """LLM 生成客户端 Protocol（供 RagAnswerService 依赖注入，便于 Mock 测试）。"""

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        api_key: str | None = None,
    ) -> str:
        """
        生成回答文本。

        Args:
            system_prompt: 系统提示词（非空字符串，定义回答约束）。
            user_prompt: 用户提示词（非空字符串，含 Context + 用户问题）。
            api_key: 当前用户的百炼 API Key（Phase 3.4 Step F6：必填，
                必须显式提供，禁止回退 settings.bailian_api_key）。

        Returns:
            strip() 后的非空回答文本。

        Raises:
            LLMClientConfigError: 配置缺失。
            LLMClientRequestError: API 调用失败。
            LLMClientResponseError: 返回结构异常。
            LLMClientEmptyResponseError: 返回内容为空。
        """
        ...


# --------------------------------------------------------------------------- 客户端实现
class BailianLLMClient:
    """
    百炼 LLM Chat Completion 客户端（OpenAI 兼容模式）。

    依赖：
        settings.bailian_base_url       → OpenAI base_url
        settings.bailian_llm_model      → 请求 model 参数（默认 qwen-plus）
        settings.bailian_llm_max_tokens → 请求 max_tokens 参数（单次回答输出 token 上限）
        （Phase 3.4 Step F6：API Key 由调用方显式传入，禁止读取 settings.bailian_api_key）
    """

    def __init__(self, settings: Settings) -> None:
        """
        构造客户端（不创建 OpenAI 连接，延迟到 generate() 调用时）。

        Args:
            settings: 配置单源实例（推荐通过 core.di.get_settings() 获取）。
        """
        self._settings: Settings = settings
        # 按 API Key 隔离的 OpenAI client 缓存（Phase 3.4 Step D）：
        # key = sha256(base_url + model + api_key)，value = OpenAI client；只存进程内存。
        # 避免用户 A 的 client 被用户 B 复用（错误使用 A 的 Key）。
        # OrderedDict 维持使用顺序：命中移到队尾，淘汰只丢最久未使用的队首。
        # _clients_lock 保护读写：generate 经 asyncio.to_thread 调用，缓存被多线程共享。
        self._clients: OrderedDict[str, OpenAI] = OrderedDict()
        self._clients_lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------ 对外入口
    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        api_key: str | None = None,
    ) -> str:
        """
        生成回答文本。

        Args:
            system_prompt: 系统提示词（定义回答约束）。
            user_prompt: 用户提示词（Context + 用户问题）。
            api_key: 当前用户的百炼 API Key（Phase 3.4 Step F6：必填，
                必须显式提供，禁止回退 settings.bailian_api_key）。

        Returns:
            strip() 后的非空回答文本。

        Raises:
            LLMClientConfigError: system_prompt / user_prompt 为空，或配置缺失。
            LLMClientRequestError: 百炼 API 调用失败（网络 / 超时 / 限流 / 5xx）。
            LLMClientResponseError: 返回结构异常（choices / message / content 缺失）。
            LLMClientEmptyResponseError: 返回内容为 None 或空字符串。
        """
        # 输入防御：prompt 必须非空
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise LLMClientConfigError("system_prompt 必须为非空字符串")
        if not isinstance(user_prompt, str) or not user_prompt.strip():
            raise LLMClientConfigError("user_prompt 必须为非空字符串")

        endpoint = self._resolve_endpoint(api_key)
        client = self._get_client(api_key)
        model = endpoint.model

        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
                max_tokens=self._settings.bailian_llm_max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 — 统一包装，保留 __cause__
            raise LLMClientRequestError(
                f"LLM API 调用失败（provider={endpoint.provider}, model={model}）：{exc}"
            ) from exc

        # 解析返回：openai SDK 1.x 的 ChatCompletion 含 .choices（list[Choice]），
        # 每个 Choice 含 .message（.content 为 str | None）。
        try:
            choices = response.choices
        except AttributeError as exc:
            raise LLMClientResponseError(
                f"LLM API 返回缺少 choices 字段：response={response!r}"
            ) from exc

        if not choices:
            raise LLMClientResponseError("LLM API 返回 choices 为空列表")

        try:
            choice = choices[0]
            content = choice.message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise LLMClientResponseError(
                f"LLM API 返回 choices[0].message.content 字段缺失或类型异常：{exc}"
            ) from exc

        if getattr(choice, "finish_reason", None) == "length":
            logger.warning(
                "LLM 回答达到 max_tokens=%d 上限被截断（model=%s），返回内容可能不完整",
                self._settings.bailian_llm_max_tokens,
                model,
            )

        if content is None:
            raise LLMClientEmptyResponseError("LLM API 返回 content 为 None")
        if not isinstance(content, str):
            raise LLMClientResponseError(
                f"LLM API content type invalid: {type(content).__name__}"
            )
        answer = content.strip()
        if not answer:
            raise LLMClientEmptyResponseError("LLM API 返回 content 为空字符串")
        return answer

    # ------------------------------------------------------------------ 内部辅助
    def _get_client(self, api_key: str | None = None) -> OpenAI:
        """
        按 API Key 隔离的 OpenAI client 获取（Phase 3.4 Step D）。

        规则：
          - api_key 必填：调用方必须显式提供当前用户的百炼 API Key（AuthService
            decrypt_api_key 解密后传入）；api_key 为 None 或空字符串 → LLMClientConfigError；
          - 严禁回退 settings.bailian_api_key（服务器 .env Key 不参与用户业务链路）；
          - 缓存 key = sha256(api_key)，不把明文 Key 当 dict key；
          - client 只存进程内存 dict，不落数据库 / Redis / 磁盘等任何持久存储；
          - 不打印 / 不记录 API Key 本身。

        经验库 153832：延迟真实连接，避免 __init__ 阶段对百炼服务的硬依赖。
        """
        endpoint = self._resolve_endpoint(api_key)
        effective_key = endpoint.api_key
        if not effective_key:
            raise LLMClientConfigError(
                "User API Key is required：调用方必须显式提供当前 Workspace 的模型凭证"
            )
        client_key = sha256_hex(
            f"{endpoint.base_url}\0{endpoint.model}\0{effective_key}"
        )
        with self._clients_lock:
            cached = self._clients.get(client_key)
            if cached is not None:
                # 命中即移到队尾：被淘汰的始终是最久未使用的 Key，而不是最早创建的 Key
                self._clients.move_to_end(client_key)
                return cached

        base_url = endpoint.base_url
        model = endpoint.model
        if not base_url or not model:
            raise LLMClientConfigError(
                f"LLM base_url 或 model 配置为空：base_url={base_url!r}, model={model!r}"
            )

        logger.info(
            "初始化 LLM OpenAI 兼容客户端：provider=%s, base_url=%s, model=%s, "
            "timeout=%ss, max_retries=%s",
            endpoint.provider,
            base_url,
            model,
            self._settings.bailian_llm_timeout_seconds,
            self._settings.bailian_max_retries,
        )
        client = OpenAI(
            api_key=effective_key,
            base_url=base_url,
            timeout=self._settings.bailian_llm_timeout_seconds,
            # 默认 2 = SDK 自身默认值；收紧会同时减少 429 限流吸收，故交运维配置而非硬编码
            max_retries=self._settings.bailian_max_retries,
        )
        with self._clients_lock:
            self._clients[client_key] = client
            evicted: OpenAI | None = None
            if len(self._clients) > _MAX_CLIENT_CACHE_SIZE:
                # 队首 = 最久未使用；先摘出再锁外关闭，避免 close 的 IO 占着锁
                _, evicted = self._clients.popitem(last=False)
        if evicted is not None:
            try:
                evicted.close()
            except Exception as exc:  # noqa: BLE001 — 关闭失败不得影响本次请求
                logger.warning("关闭被淘汰的 LLM OpenAI 客户端失败：%s", exc)
        return client

    def _resolve_endpoint(
        self, api_key: str | None
    ) -> ModelEndpointCredential:
        if isinstance(api_key, WorkspaceModelCredentials):
            return api_key.llm
        return ModelEndpointCredential(
            provider="dashscope",
            api_key=str(api_key or ""),
            base_url=self._settings.bailian_base_url,
            model=self._settings.bailian_llm_model,
            send_dimensions=False,
        )
