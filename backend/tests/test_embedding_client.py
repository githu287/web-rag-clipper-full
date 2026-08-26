"""EmbeddingClient 单元测试（Phase 2.14 Step 5 补齐）。

策略：Mock 百炼 API（unittest.mock），不发起任何真实外部调用。
- 使用 Settings(_env_file=None) 构造全 mock 配置，不依赖真实 .env / 网络 / API Key。
- 通过 patch EmbeddingClient._get_client 注入 fake OpenAI 客户端，验证请求参数 / batch 行为 /
  异常转换 / dimension 校验。

覆盖（Phase 2.14 要求 A-J）：
  A. 正常 embed（请求参数 model/input/dimensions + 返回向量）
  B. 空字符串输入
  C. 空列表输入（不调用 API）
  D. 非字符串输入
  E. batch size 分批行为（分批大小、调用次数、顺序保持）
  F. embedding dimension 校验
  G. API 异常包装（openai 异常 → EmbeddingAPIError）
  H. 返回值数量与输入数量一致
  I. 配置错误（API Key / base_url / model 缺失 → EmbeddingConfigError）
  J. 异常链保留（__cause__）+ 返回结构异常（缺 data / 条数不匹配 / 缺 index / 缺 embedding）

注：EmbeddingClient 为纯客户端封装，__init__ 惰性连接（经验库 153832），因此上述场景均可无外部
依赖 mock；若未来引入连接池等外部资源导致无法 mock，才需记录设计变更。
"""
from __future__ import annotations

import unittest
from unittest import mock

import httpx
from openai import APIConnectionError

from backend.clients.embedding import (
    EmbeddingAPIError,
    EmbeddingClient,
    EmbeddingConfigError,
    EmbeddingResponseError,
)
from backend.core.config import Settings

_EMBED_DIM = 4  # 测试用小维度（生产为 1024，由 Settings 注入）


def _make_settings(**overrides) -> Settings:
    """构造不读取 .env 的测试 Settings（配置单源，全 mock）。"""
    defaults: dict = dict(
        bailian_api_key="test-key",
        bailian_base_url="https://example.com/v1",
        bailian_embedding_model="test-embedding-model",
        bailian_embedding_dimension=_EMBED_DIM,
        embedding_batch_size=2,
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


class _FakeEmbeddingData:
    """模拟 openai SDK 的 EmbeddingData（index + embedding）。"""

    def __init__(self, index: int, embedding: list[float]):
        self.index = index
        self.embedding = embedding


class _FakeEmbeddingResponse:
    """模拟 openai SDK 的 EmbeddingResponse（仅暴露 .data）。"""

    def __init__(self, data: list):
        self.data = data


def _dim_vector(seed: float = 0.1, dim: int = _EMBED_DIM) -> list[float]:
    return [seed] * dim


class EmbeddingClientTest(unittest.TestCase):
    """A-H / J：注入 fake OpenAI client（patch _get_client）。"""

    def setUp(self) -> None:
        self.settings = _make_settings()
        self.client = EmbeddingClient(self.settings)
        self.fake_openai = mock.MagicMock()
        patcher = mock.patch.object(
            EmbeddingClient, "_get_client", return_value=self.fake_openai
        )
        self.addCleanup(patcher.stop)
        patcher.start()

    # ---------------------------------------------------------------- A. 正常 embed
    def test_a_embed_ok_returns_vectors_and_passes_params(self) -> None:
        self.fake_openai.embeddings.create.return_value = _FakeEmbeddingResponse(
            [
                _FakeEmbeddingData(0, _dim_vector(0.1)),
                _FakeEmbeddingData(1, _dim_vector(0.2)),
            ]
        )
        vectors = self.client.embed(["hello", "world"])

        self.assertEqual(len(vectors), 2)
        self.assertEqual(vectors[0], _dim_vector(0.1))
        self.assertEqual(vectors[1], _dim_vector(0.2))
        self.fake_openai.embeddings.create.assert_called_once_with(
            model="test-embedding-model",
            input=["hello", "world"],
            dimensions=_EMBED_DIM,
        )

    # ---------------------------------------------------------------- B. 空字符串输入
    def test_b_empty_string_rejected(self) -> None:
        # 注：客户端校验约束为「类型 str 且 len>=1」，纯空格串（len>=1）不在此拦截，
        # 属既定设计（校验语义为「非空」，空格为合法输入）。
        with self.assertRaises(EmbeddingConfigError):
            self.client.embed(["", "ok"])
        # 校验在 API 调用之前，未发生外部调用
        self.fake_openai.embeddings.create.assert_not_called()

    # ---------------------------------------------------------------- C. 空列表输入
    def test_c_empty_list_returns_empty_without_api_call(self) -> None:
        self.assertEqual(self.client.embed([]), [])
        self.fake_openai.embeddings.create.assert_not_called()

    # ---------------------------------------------------------------- D. 非字符串输入
    def test_d_non_string_input_rejected(self) -> None:
        for bad in [123, 1.5, None, b"bytes", ["nested"]]:
            with self.assertRaises(EmbeddingConfigError):
                self.client.embed([bad])
        self.fake_openai.embeddings.create.assert_not_called()

    # ---------------------------------------------------------------- E. batch size 分批
    def test_e_batch_splitting_order_preserved(self) -> None:
        # batch_size=2，输入 5 条 → 3 次调用（2 / 2 / 1）
        self.fake_openai.embeddings.create.side_effect = [
            _FakeEmbeddingResponse(
                [_FakeEmbeddingData(0, _dim_vector(0.1)), _FakeEmbeddingData(1, _dim_vector(0.1))]
            ),
            _FakeEmbeddingResponse(
                [_FakeEmbeddingData(0, _dim_vector(0.2)), _FakeEmbeddingData(1, _dim_vector(0.2))]
            ),
            _FakeEmbeddingResponse([_FakeEmbeddingData(0, _dim_vector(0.3))]),
        ]
        vectors = self.client.embed(["a", "b", "c", "d", "e"])

        self.assertEqual(len(vectors), 5)
        calls = self.fake_openai.embeddings.create.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0].kwargs["input"], ["a", "b"])
        self.assertEqual(calls[1].kwargs["input"], ["c", "d"])
        self.assertEqual(calls[2].kwargs["input"], ["e"])
        # 结果按输入顺序拼接，不重排
        self.assertEqual(vectors[0], _dim_vector(0.1))
        self.assertEqual(vectors[2], _dim_vector(0.2))
        self.assertEqual(vectors[4], _dim_vector(0.3))

    def test_e_index_order_normalized_by_index(self) -> None:
        # 百炼偶发乱序返回：data 顺序 [1, 0]，应按 index 排序还原输入顺序
        self.fake_openai.embeddings.create.return_value = _FakeEmbeddingResponse(
            [
                _FakeEmbeddingData(1, _dim_vector(0.9)),
                _FakeEmbeddingData(0, _dim_vector(0.1)),
            ]
        )
        vectors = self.client.embed(["a", "b"])
        self.assertEqual(vectors[0], _dim_vector(0.1))
        self.assertEqual(vectors[1], _dim_vector(0.9))

    # ---------------------------------------------------------------- F. dimension 校验
    def test_f_dimension_mismatch_raises_response_error(self) -> None:
        # 返回 3 维向量，期望 4 维 → EmbeddingResponseError（不可重试）
        self.fake_openai.embeddings.create.return_value = _FakeEmbeddingResponse(
            [_FakeEmbeddingData(0, _dim_vector(0.1, dim=3))]
        )
        with self.assertRaises(EmbeddingResponseError) as ctx:
            self.client.embed(["x"])
        self.assertIn("维度", str(ctx.exception))
        self.assertIn("3", str(ctx.exception))

    # ---------------------------------------------------------------- G. API 异常包装
    def test_g_api_error_wrapped_with_cause(self) -> None:
        self.fake_openai.embeddings.create.side_effect = APIConnectionError(
            request=httpx.Request("POST", "https://example.com/v1/embeddings")
        )
        with self.assertRaises(EmbeddingAPIError) as ctx:
            self.client.embed(["x"])
        self.assertIsInstance(ctx.exception.__cause__, APIConnectionError)

    # ---------------------------------------------------------------- H. 返回数量一致
    def test_h_return_count_matches_input(self) -> None:
        texts = [f"t{i}" for i in range(7)]  # batch_size=2 → 4 批（2/2/2/1）
        batches = [texts[i : i + 2] for i in range(0, len(texts), 2)]
        self.fake_openai.embeddings.create.side_effect = [
            _FakeEmbeddingResponse(
                [_FakeEmbeddingData(j, _dim_vector()) for j in range(len(batch))]
            )
            for batch in batches
        ]
        vectors = self.client.embed(texts)
        self.assertEqual(len(vectors), len(texts))

    # ---------------------------------------------------------------- J. 结构异常 + 异常链
    def test_j_response_missing_data_field(self) -> None:
        resp = mock.MagicMock()
        del resp.data  # 模拟返回对象无 data 属性
        self.fake_openai.embeddings.create.return_value = resp
        with self.assertRaises(EmbeddingResponseError) as ctx:
            self.client.embed(["x"])
        self.assertIsInstance(ctx.exception.__cause__, AttributeError)

    def test_j_count_mismatch(self) -> None:
        # 输入 2 条，返回 1 条 → EmbeddingResponseError
        self.fake_openai.embeddings.create.return_value = _FakeEmbeddingResponse(
            [_FakeEmbeddingData(0, _dim_vector())]
        )
        with self.assertRaises(EmbeddingResponseError) as ctx:
            self.client.embed(["a", "b"])
        self.assertIn("不匹配", str(ctx.exception))

    def test_j_missing_index_field(self) -> None:
        item = mock.MagicMock()
        del item.index
        self.fake_openai.embeddings.create.return_value = _FakeEmbeddingResponse([item])
        with self.assertRaises(EmbeddingResponseError) as ctx:
            self.client.embed(["x"])
        self.assertIsInstance(ctx.exception.__cause__, AttributeError)

    def test_j_missing_embedding_field(self) -> None:
        item = mock.MagicMock(index=0)
        del item.embedding
        self.fake_openai.embeddings.create.return_value = _FakeEmbeddingResponse([item])
        with self.assertRaises(EmbeddingResponseError) as ctx:
            self.client.embed(["x"])
        self.assertIsInstance(ctx.exception.__cause__, AttributeError)


class EmbeddingClientConfigTest(unittest.TestCase):
    """I. 配置错误：不 mock _get_client，走真实惰性校验路径。"""

    def test_i_missing_api_key_raises_config_error(self) -> None:
        # Phase 3.4 Step F6：api_key 必须显式提供，不再回退 settings.bailian_api_key
        client = EmbeddingClient(_make_settings())
        with self.assertRaises(EmbeddingConfigError) as ctx:
            client.embed(["x"])
        self.assertIn("User API Key is required", str(ctx.exception))

    def test_i_missing_base_url_raises_config_error(self) -> None:
        # F6：必须显式传入 api_key，才能走到 base_url 校验分支
        client = EmbeddingClient(_make_settings(bailian_base_url=""))
        with self.assertRaises(EmbeddingConfigError):
            client.embed(["x"], api_key="test-user-key")

    def test_i_missing_model_raises_config_error(self) -> None:
        # 设计说明：Settings.bailian_embedding_model 带 min_length=1，空值在 Settings 构造期即被
        # pydantic 拦截（生产路径更早失败）；EmbeddingClient._get_client 中的空 model 校验是
        # 冗余防御分支，此处绕过构造校验直接验证该分支。
        client = EmbeddingClient(_make_settings())
        client._settings.bailian_embedding_model = ""  # noqa: SLF001 — 测试绕过 pydantic 前置校验
        with self.assertRaises(EmbeddingConfigError):
            client.embed(["x"], api_key="test-user-key")


class EmbeddingClientUserKeyTest(unittest.TestCase):
    """Phase 3.4 Step F6：用户 API Key 必须显式传入，且按 Key 隔离缓存。"""

    def setUp(self) -> None:
        self.patcher = mock.patch("backend.clients.embedding.OpenAI")
        self.mock_openai_cls = self.patcher.start()
        self.addCleanup(self.patcher.stop)
        # 显式放置一个「服务器 Key」，用于断言它绝不被用户链路使用
        self.settings = _make_settings(bailian_api_key="server-key")
        self.client = EmbeddingClient(self.settings)

    def test_user_api_key_used_in_client(self) -> None:
        """F6-A：显式用户 Key → OpenAI 以该 Key 构造（不读取 settings.bailian_api_key）。"""
        self.client._get_client("sk-user-a")
        self.mock_openai_cls.assert_called_once_with(
            api_key="sk-user-a",
            base_url="https://example.com/v1",
        )

    def test_missing_api_key_raises_config_error(self) -> None:
        """F6-B：api_key=None → EmbeddingConfigError，且不构造 OpenAI。"""
        with self.assertRaises(EmbeddingConfigError) as ctx:
            self.client._get_client(None)
        self.assertIn("User API Key is required", str(ctx.exception))
        self.mock_openai_cls.assert_not_called()

    def test_empty_api_key_raises_config_error(self) -> None:
        """F6-B：api_key="" → EmbeddingConfigError，且不构造 OpenAI。"""
        with self.assertRaises(EmbeddingConfigError):
            self.client._get_client("")
        self.mock_openai_cls.assert_not_called()

    def test_different_keys_isolated_clients(self) -> None:
        """F6-D：Key A / Key B → 两个独立 OpenAI client（cache 按 Key 隔离）。"""
        self.client._get_client("sk-user-a")
        self.client._get_client("sk-user-b")
        # OpenAI 构造了 2 次，且每次使用各自 Key
        self.assertEqual(self.mock_openai_cls.call_count, 2)
        calls = self.mock_openai_cls.call_args_list
        self.assertEqual(calls[0][1]["api_key"], "sk-user-a")
        self.assertEqual(calls[1][1]["api_key"], "sk-user-b")
        # cache 有 2 个独立条目（Key A / Key B 各占一条）
        self.assertEqual(len(self.client._clients), 2)  # noqa: SLF001

    def test_same_key_shared_client(self) -> None:
        """F6-E：相同 Key → 共享同一 OpenAI client（不重复构造）。"""
        client_a1 = self.client._get_client("sk-user-a")
        client_a2 = self.client._get_client("sk-user-a")
        self.assertIs(client_a1, client_a2)
        self.assertEqual(self.mock_openai_cls.call_count, 1)
        self.assertEqual(len(self.client._clients), 1)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
