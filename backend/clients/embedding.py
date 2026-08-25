"""
百炼 Embedding API 客户端封装（Phase 2.5 Step 2）。

职责：
    文本列表 → 百炼 OpenAI 兼容 Embedding API → list[list[float]]

严格不负责：
    - 文档解析 / 切片（Service 层职责）
    - Milvus 写入（Repository 层职责）
    - RAG 流程编排（Service 层职责）
    - Cache（本阶段不引入）
    - 业务状态管理

设计要点（经验库 153832 规则应用）：
1) 配置单源：API Key / base_url / model / dimension / batch_size 全部从 Settings 注入，禁止硬编码。
2) 延迟真实连接：__init__ 仅保存配置，不创建 OpenAI client；embed() 调用时才惰性创建。
   保证「无 .env / 无网络」也能 import 本模块、能构造对象（便于单元测试与启动期装配）。
3) 分批硬限制：百炼 text-embedding-v3 单次请求最大 10 行；本客户端内部按 settings.embedding_batch_size
   自动分批，调用方传入任意长度列表均安全；返回结果按原顺序拼接，不重排。
4) 异常不吞：openai SDK 抛出的所有异常统一包装为 EmbeddingClientError 族并保留 __cause__。
5) 维度防御：每次返回前强校验每条向量长度 == settings.bailian_embedding_dimension，
   防止百炼返回异常维度向量被静默写入 Milvus（经验库 610470 DataNotMatchException 前置防御）。
"""

from __future__ import annotations

import logging
from typing import Final

from openai import OpenAI

from ..core.config import Settings
from ..core.security import sha256_hex

logger: logging.Logger = logging.getLogger(__name__)

# Embedding 输入文本非空校验用的最小长度（避免空字符串触发百炼 400）
_TEXT_MIN_LENGTH: Final[int] = 1


# --------------------------------------------------------------------------- 异常定义
class EmbeddingClientError(Exception):
    """Embedding 客户端异常根类（与 core.exceptions.MilvusRepositoryError 平级的外部客户端异常族）。"""


class EmbeddingConfigError(EmbeddingClientError):
    """配置类错误（API Key 为空 / base_url 非法 / model 未设置等），不可重试。"""


class EmbeddingAPIError(EmbeddingClientError):
    """百炼 API 调用类错误（网络 / 超时 / 限流 / 5xx / 返回结构异常），可由上层决定重试。"""


class EmbeddingResponseError(EmbeddingClientError):
    """百炼返回契约不一致（向量维度不符 / 返回条数与输入不匹配 / 字段缺失），不可重试。"""


# --------------------------------------------------------------------------- 客户端实现
class EmbeddingClient:
    """
    百炼 text-embedding-v3 嵌入客户端（OpenAI 兼容模式）。

    依赖：
        settings.bailian_api_key            → OpenAI api_key
        settings.bailian_base_url           → OpenAI base_url
        settings.bailian_embedding_model    → 请求 model 参数
        settings.bailian_embedding_dimension → 返回维度强校验基准（默认 1024）
        settings.embedding_batch_size       → 自动分批大小（≤10，百炼硬限制）

    使用方式（后续 Service 层示例，本阶段不实现）：
        client = EmbeddingClient(settings)
        vectors = client.embed(["hello", "world"])  # → [[...1024...], [...1024...]]
    """

    def __init__(self, settings: Settings) -> None:
        """
        构造客户端（不创建 OpenAI 连接，延迟到 embed() 调用时）。

        Args:
            settings: 配置单源实例（推荐通过 core.di.get_settings() 获取）。
        """
        self._settings: Settings = settings
        # 按 API Key 隔离的 OpenAI client 缓存（Phase 3.4 Step D）：
        # key = sha256(api_key)，value = OpenAI client；只存进程内存。
        # 避免用户 A 的 client 被用户 B 复用（错误使用 A 的 Key）。
        self._clients: dict[str, OpenAI] = {}

    # ------------------------------------------------------------------ 对外入口
    def embed(
        self,
        texts: list[str],
        api_key: str | None = None,
    ) -> list[list[float]]:
        """
        将文本列表转换为向量列表（顺序与输入一一对应）。

        行为：
          - 空列表：直接返回 []，不调用 API。
          - 自动分批：按 settings.embedding_batch_size（≤10）切片逐批请求，结果按原顺序拼接。
          - 维度校验：每条返回向量长度必须 == settings.bailian_embedding_dimension，否则抛
            EmbeddingResponseError（不可重试；避免维度错向量被写入 Milvus）。

        Args:
            texts: 待嵌入的文本列表；每条必须非空字符串。
            api_key: 百炼 API Key（Phase 3.4 Step D：用户自己的 Key，由
                AuthService 解密后经 Service 传入）；None 时回退
                settings.bailian_api_key（仅本地测试 / 兼容旧调用）。

        Returns:
            list[list[float]]：与 texts 等长、同序；每条向量长度 == bailian_embedding_dimension。

        Raises:
            EmbeddingConfigError: API Key 未配置 / model 名为空等配置类错误。
            EmbeddingAPIError: 百炼 API 调用失败（网络 / 超时 / 限流 / 5xx）。
            EmbeddingResponseError: 返回条数不匹配 / 维度不匹配 / 结构异常。
        """
        if not texts:
            return []

        # 输入校验：每条文本必须非空字符串
        for idx, text in enumerate(texts):
            if not isinstance(text, str) or len(text) < _TEXT_MIN_LENGTH:
                raise EmbeddingConfigError(
                    f"EmbeddingClient.embed 输入 texts[{idx}] 非法：必须为非空字符串，实际={text!r}"
                )

        batch_size = self._settings.embedding_batch_size
        dim = self._settings.bailian_embedding_dimension
        all_vectors: list[list[float]] = []

        # 按 batch_size 切片，逐批请求
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            batch_vectors = self._embed_batch(batch, dim, api_key)
            all_vectors.extend(batch_vectors)

        return all_vectors

    # ------------------------------------------------------------------ 内部辅助
    def _get_client(self, api_key: str | None = None) -> OpenAI:
        """
        按 API Key 隔离的 OpenAI client 获取（Phase 3.4 Step D）。

        规则：
          - api_key 提供 → 使用该 Key（用户自己的百炼 Key；AuthService 解密后传入）；
          - api_key 为 None → 回退 settings.bailian_api_key（仅本地测试 / 兼容旧调用）；
          - 缓存 key = sha256(api_key)，不把明文 Key 当 dict key；
          - client 只存进程内存 dict，不落数据库 / Redis / 磁盘等任何持久存储；
          - 不打印 / 不记录 API Key 本身。

        经验库 153832：延迟真实连接，避免 import 阶段 / __init__ 阶段对百炼服务的硬依赖。
        """
        effective_key = api_key if api_key else self._settings.bailian_api_key
        if not effective_key:
            raise EmbeddingConfigError(
                "API Key 未配置：请检查环境变量 BAILIAN_API_KEY 或传入用户 Key"
            )

        client_key = sha256_hex(effective_key)
        cached = self._clients.get(client_key)
        if cached is not None:
            return cached

        base_url = self._settings.bailian_base_url
        model = self._settings.bailian_embedding_model
        if not base_url or not model:
            raise EmbeddingConfigError(
                f"bailian_base_url 或 bailian_embedding_model 配置为空：base_url={base_url!r}, model={model!r}"
            )

        logger.info(
            "初始化百炼 Embedding OpenAI 兼容客户端：base_url=%s, model=%s",
            base_url,
            model,
        )
        client = OpenAI(api_key=effective_key, base_url=base_url)
        self._clients[client_key] = client
        return client

    def _embed_batch(
        self,
        batch: list[str],
        expected_dim: int,
        api_key: str | None = None,
    ) -> list[list[float]]:
        """
        调用百炼 Embedding API 处理一个批次（≤10 条文本）。

        Args:
            batch: 单批文本列表（长度 ≤ settings.embedding_batch_size）。
            expected_dim: 期望的向量维度（settings.bailian_embedding_dimension）。

        Returns:
            该批次的向量列表，与 batch 等长同序。

        Raises:
            EmbeddingAPIError: API 调用失败。
            EmbeddingResponseError: 返回结构/条数/维度不匹配。
        """
        client = self._get_client(api_key)
        model = self._settings.bailian_embedding_model

        try:
            response = client.embeddings.create(
                model=model,
                input=batch,
                dimensions=expected_dim,
            )
        except Exception as exc:  # noqa: BLE001 — 统一包装，保留 __cause__
            raise EmbeddingAPIError(
                f"百炼 Embedding API 调用失败（model={model}, batch_size={len(batch)}）：{exc}"
            ) from exc

        # 解析返回：openai SDK 1.x 的 EmbeddingResponse.data 是 list[EmbeddingData]，每条含 .embedding
        try:
            data_items = response.data
        except AttributeError as exc:
            raise EmbeddingResponseError(
                f"百炼返回缺少 data 字段：response={response!r}"
            ) from exc

        if len(data_items) != len(batch):
            raise EmbeddingResponseError(
                f"百炼返回条数 {len(data_items)} 与输入 {len(batch)} 不匹配"
            )

        # 按 index 排序防御（百炼通常按输入顺序返回，但规范上应按 response.data[].index 排序后取 embedding）
        try:
            sorted_items = sorted(data_items, key=lambda d: d.index)
        except (AttributeError, TypeError) as exc:
            raise EmbeddingResponseError(
                f"百炼返回 data[].index 字段缺失或类型异常：{exc}"
            ) from exc

        vectors: list[list[float]] = []
        for item in sorted_items:
            try:
                vec = list(item.embedding)
            except (AttributeError, TypeError) as exc:
                raise EmbeddingResponseError(
                    f"百炼返回 data[].embedding 字段缺失或类型异常：{exc}"
                ) from exc

            if len(vec) != expected_dim:
                raise EmbeddingResponseError(
                    f"百炼返回向量维度 {len(vec)} 与期望 {expected_dim} 不匹配"
                    "（不可重试；请检查 BAILIAN_EMBEDDING_DIMENSION 与 Milvus Collection dim 是否一致）"
                )
            vectors.append(vec)

        return vectors
