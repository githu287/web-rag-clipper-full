"""
BailianLLMClient 单元测试（Phase 3.3 Step 3）。

技术栈：unittest + unittest.mock。
不连接真实百炼 API：
    - 通过 patch("backend.clients.llm.OpenAI") 将 OpenAI 替换为 Mock；
    - 验证 generate() 的参数构造 / 返回 strip / 异常族 / 异常链 / 惰性创建。

覆盖场景（对应 Phase 3.3 Step 3 §二十二 LLM Client 测试要求）：
    1. 正常返回
    2. strip
    3. empty response
    4. None response
    5. API error
    6. response structure error
    7. API key missing
    8. model missing
    9. exception chain
"""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from backend.clients.llm import (
    BailianLLMClient,
    LLMClientConfigError,
    LLMClientEmptyResponseError,
    LLMClientRequestError,
    LLMClientResponseError,
)
from backend.core.config import Settings


def _make_settings(**overrides: object) -> Settings:
    """构造测试用 Settings（默认注入有效 API Key，可按需覆盖）。"""
    defaults: dict[str, object] = {
        "bailian_api_key": "test-api-key",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _make_client_response(content: object | None) -> Mock:
    """构造 openai SDK 风格的 ChatCompletion 返回对象（Mock 树）。"""
    message = Mock()
    message.content = content
    choice = Mock()
    choice.message = message
    completion = Mock()
    completion.choices = [choice]
    return completion


class BailianLLMClientTest(unittest.TestCase):
    """BailianLLMClient.generate() 单元测试。"""

    def setUp(self) -> None:
        """每个用例：patch OpenAI，构造可注入 client 的响应。"""
        self.patcher = patch("backend.clients.llm.OpenAI")
        self.mock_openai_cls = self.patcher.start()
        self.mock_client = Mock()
        self.mock_openai_cls.return_value = self.mock_client

    def tearDown(self) -> None:
        self.patcher.stop()

    def _make_client(self, settings: Settings | None = None) -> BailianLLMClient:
        return BailianLLMClient(settings or _make_settings())

    # ----------------------------------------------------- 1. 正常返回
    def test_generate_success_returns_stripped_text(self) -> None:
        """1+2：正常返回 + strip：返回去除首尾空白后的非空文本。"""
        self.mock_client.chat.completions.create.return_value = (
            _make_client_response("  \n  你好，这是回答内容。  \n")
        )
        client = self._make_client()

        result = client.generate("system prompt", "user prompt", api_key="test-user-key")

        self.assertEqual(result, "你好，这是回答内容。")
        # 断言请求参数：model / messages（system + user）/ temperature
        self.mock_client.chat.completions.create.assert_called_once_with(
            model="qwen-plus",
            messages=[
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "user prompt"},
            ],
            temperature=0.2,
        )

    # ----------------------------------------------------- 2. strip 由用例 1 覆盖；补充内嵌空白保留
    def test_generate_keeps_inner_whitespace(self) -> None:
        """strip 只去除首尾空白，不破坏正文内部空白。"""
        self.mock_client.chat.completions.create.return_value = (
            _make_client_response("第一段\n\n第二段")
        )
        client = self._make_client()

        result = client.generate("s", "u", api_key="test-user-key")

        self.assertEqual(result, "第一段\n\n第二段")

    # ----------------------------------------------------- 3. empty response
    def test_generate_empty_string_raises_empty_response(self) -> None:
        """3：content 为空字符串（strip 后为空）→ LLMClientEmptyResponseError。"""
        self.mock_client.chat.completions.create.return_value = (
            _make_client_response("   \n  ")
        )
        client = self._make_client()

        with self.assertRaises(LLMClientEmptyResponseError):
            client.generate("s", "u", api_key="test-user-key")

    # ----------------------------------------------------- 4. None response
    def test_generate_none_content_raises_empty_response(self) -> None:
        """4：content 为 None → LLMClientEmptyResponseError。"""
        self.mock_client.chat.completions.create.return_value = (
            _make_client_response(None)
        )
        client = self._make_client()

        with self.assertRaises(LLMClientEmptyResponseError):
            client.generate("s", "u", api_key="test-user-key")

    # ----------------------------------------------------- 5. API error
    def test_generate_api_error_wrapped_as_request_error(self) -> None:
        """5：底层 API 异常 → LLMClientRequestError，且保留异常链 __cause__。"""
        self.mock_client.chat.completions.create.side_effect = TimeoutError(
            "connection timeout"
        )
        client = self._make_client()

        with self.assertRaises(LLMClientRequestError) as ctx:
            client.generate("s", "u", api_key="test-user-key")

        self.assertIsInstance(ctx.exception.__cause__, TimeoutError)
        self.assertIn("connection timeout", str(ctx.exception))

    # ----------------------------------------------------- 6. response structure error
    def test_generate_missing_choices_raises_response_error(self) -> None:
        """6a：response 无 choices 属性 → LLMClientResponseError，保留异常链。"""
        completion = Mock()
        del completion.choices  # 模拟响应契约缺字段
        self.mock_client.chat.completions.create.return_value = completion
        client = self._make_client()

        with self.assertRaises(LLMClientResponseError) as ctx:
            client.generate("s", "u", api_key="test-user-key")

        self.assertIsInstance(ctx.exception.__cause__, AttributeError)

    def test_generate_empty_choices_raises_response_error(self) -> None:
        """6b：choices 为空列表 → LLMClientResponseError。"""
        completion = Mock()
        completion.choices = []
        self.mock_client.chat.completions.create.return_value = completion
        client = self._make_client()

        with self.assertRaises(LLMClientResponseError):
            client.generate("s", "u", api_key="test-user-key")

    def test_generate_missing_message_raises_response_error(self) -> None:
        """6c：choices[0] 无 message → LLMClientResponseError，保留异常链。"""
        choice = Mock()
        del choice.message
        completion = Mock()
        completion.choices = [choice]
        self.mock_client.chat.completions.create.return_value = completion
        client = self._make_client()

        with self.assertRaises(LLMClientResponseError) as ctx:
            client.generate("s", "u", api_key="test-user-key")

        self.assertIsInstance(ctx.exception.__cause__, AttributeError)

    # ----------------------------------------------------- 7. API key missing
    def test_generate_missing_api_key_raises_config_error(self) -> None:
        """7：api_key 未显式提供 → LLMClientConfigError（不触发网络调用）。"""
        client = self._make_client()

        with self.assertRaises(LLMClientConfigError):
            client.generate("s", "u")

        # 配置错误发生在 _get_client() 阶段，OpenAI 不应被构造
        self.mock_openai_cls.assert_not_called()

    # ----------------------------------------------------- 8. model missing
    def test_generate_missing_model_raises_config_error(self) -> None:
        """8：model 为空 → LLMClientConfigError（不触发网络调用）。"""
        # Field(min_length=1) 会拒绝空字符串构造，故先构造有效 Settings 再篡改字段
        # 以模拟「配置缺失 / 运行期被置空」的场景。
        settings = _make_settings()
        settings.bailian_llm_model = ""  # 绕过构造校验，模拟配置缺失
        client = self._make_client(settings)

        with self.assertRaises(LLMClientConfigError):
            client.generate("s", "u", api_key="test-user-key")

        self.mock_openai_cls.assert_not_called()

    # ----------------------------------------------------- 9. exception chain
    def test_generate_exception_chain_preserved(self) -> None:
        """9：异常链保留：LLMClientRequestError.__cause__ 为原始 API 异常。"""
        original = ConnectionError("refused")
        self.mock_client.chat.completions.create.side_effect = original
        client = self._make_client()

        with self.assertRaises(LLMClientRequestError) as ctx:
            client.generate("s", "u", api_key="test-user-key")

        self.assertIs(ctx.exception.__cause__, original)

    # ----------------------------------------------------- 额外：惰性创建
    def test_client_created_lazily_on_first_generate(self) -> None:
        """惰性创建：__init__ 不构造 OpenAI client，首次 generate 才构造。"""
        self.mock_client.chat.completions.create.return_value = (
            _make_client_response("ok")
        )
        client = self._make_client()
        self.mock_openai_cls.assert_not_called()

        client.generate("s", "u", api_key="test-user-key")
        self.mock_openai_cls.assert_called_once()
        # 第二次调用复用同一 client（相同 Key 不重复构造）
        client.generate("s", "u", api_key="test-user-key")
        self.assertEqual(self.mock_openai_cls.call_count, 1)

    # ----------------------------------------------------- 额外：prompt 空值防御
    def test_generate_empty_prompt_raises_config_error(self) -> None:
        """空 prompt（service 层防御）→ LLMClientConfigError，不触发网络调用。"""
        client = self._make_client()

        with self.assertRaises(LLMClientConfigError):
            client.generate("  ", "u")
        with self.assertRaises(LLMClientConfigError):
            client.generate("s", "")

        self.mock_openai_cls.assert_not_called()


class BailianLLMClientUserKeyTest(unittest.TestCase):
    """Phase 3.4 Step F6：用户 API Key 必须显式传入且按 Key 隔离缓存。"""

    def setUp(self) -> None:
        self.patcher = patch("backend.clients.llm.OpenAI")
        self.mock_openai_cls = self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.mock_client = Mock()
        self.mock_openai_cls.return_value = self.mock_client
        self.mock_client.chat.completions.create.return_value = _make_client_response(
            "ok"
        )
        # 显式放置「服务器 Key」，断言用户链路绝不使用它
        self.client = BailianLLMClient(_make_settings(bailian_api_key="server-key"))

    def test_user_api_key_used_in_client(self) -> None:
        """F6-A：显式用户 Key → OpenAI 以该 Key 构造（不读取 settings.bailian_api_key）。"""
        self.client.generate("s", "u", api_key="sk-user-a")
        _, kwargs = self.mock_openai_cls.call_args
        self.assertEqual(kwargs["api_key"], "sk-user-a")
        self.assertNotEqual(kwargs["api_key"], "server-key")

    def test_missing_api_key_raises_config_error(self) -> None:
        """F6-B：api_key 缺失 → LLMClientConfigError，不构造 OpenAI。"""
        with self.assertRaises(LLMClientConfigError) as ctx:
            self.client.generate("s", "u")
        self.assertIn("User API Key is required", str(ctx.exception))
        self.mock_openai_cls.assert_not_called()

    def test_different_keys_isolated_clients(self) -> None:
        """F6-D：Key A / Key B → 两个独立 OpenAI client（cache 按 Key 隔离）。"""
        self.client.generate("s", "u", api_key="sk-user-a")
        self.client.generate("s", "u", api_key="sk-user-b")
        self.assertEqual(self.mock_openai_cls.call_count, 2)
        self.assertEqual(len(self.client._clients), 2)  # noqa: SLF001

    def test_same_key_shared_client(self) -> None:
        """F6-E：相同 Key → 共享同一 OpenAI client（不重复构造）。"""
        self.client.generate("s", "u", api_key="sk-user-a")
        self.client.generate("s", "u", api_key="sk-user-a")
        self.assertEqual(self.mock_openai_cls.call_count, 1)
        self.assertEqual(len(self.client._clients), 1)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
