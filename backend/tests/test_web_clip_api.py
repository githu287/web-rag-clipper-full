"""
Web Clip API 测试（Phase 3.1 Step 3 Schema 校验 + POST /clips HTTP 级覆盖）。

覆盖用例（对应 Step 3 §十 测试要求 8~9 及契约）：
  A. WebClipRequest：url / raw_text 必填（缺字段 → ValidationError）。
  B. url 空字符串 → ValidationError（min_length=1）。
  C. url 超长（>2048）→ ValidationError（max_length=2048）。
  D. raw_text 空字符串 → ValidationError（min_length=1）。
  E. title 超长（>512）→ ValidationError（max_length=512）。
  F. title=None / 缺省 → 合法。
  G. extra 字段（如 source_type）→ ValidationError（extra="forbid"，
     禁止客户端伪造 source_type，与 WebClip 契约一致）。
  H. WebClipResponse 全字段组装（8 字段契约校验）。
  I. WebClipRequest 合法示例正常解析。

HTTP 级覆盖（WebClipApiTest，补优化 #24b/#24c 遗留的同步剪藏入口缺口）：
  1. 201 成功：来源元数据 8 字段透传 + clip() 收到的 kwargs（含 plugin_id /
     解密后的 api_key）与 decrypt_api_key 调用一次。
  2. DocumentQuotaExceededError → 429（type / 中文 detail 原样透传给扩展端）。
  3. 缺 X-Plugin-ID / X-Plugin-Secret → 401 且不进入剪藏链路。
  4. 客户端伪造 source_type → 422（extra="forbid" 在 HTTP 边界同样生效）。

技术栈：A~I 为 Pydantic v2 模型直接校验（无需 TestClient / 数据库）；
WebClipApiTest 用 FastAPI TestClient + dependency_overrides 替换
get_web_clip_service / get_plugin_service，并以
patch("backend.main.get_milvus_initializer") 阻断 lifespan 的真实 Milvus 连接，
不连 MySQL / Milvus / 百炼 / Redis。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.core.di import get_plugin_service, get_web_clip_service
from backend.core.exceptions import DocumentQuotaExceededError
from backend.main import create_app
from backend.models.document_api_schema import WebClipRequest, WebClipResponse

_VALID_URL = "https://example.com/article/1"
_VALID_TEXT = "网页正文纯文本"


class WebClipRequestValidationTest(unittest.TestCase):
    """WebClipRequest 校验规则测试。"""

    # ---------------------------------------------- A. 必填字段缺失
    def test_a_url_required(self) -> None:
        """A1：缺 url → ValidationError。"""
        with self.assertRaises(ValidationError):
            WebClipRequest(raw_text=_VALID_TEXT)

    def test_a_raw_text_required(self) -> None:
        """A2：缺 raw_text → ValidationError。"""
        with self.assertRaises(ValidationError):
            WebClipRequest(url=_VALID_URL)

    # ---------------------------------------------- B. url 非空（min_length=1）
    def test_b_url_empty_rejected(self) -> None:
        """B：url="" → ValidationError。"""
        with self.assertRaises(ValidationError):
            WebClipRequest(url="", raw_text=_VALID_TEXT)

    # ---------------------------------------------- C. url 超长（max_length=2048）
    def test_c_url_too_long_rejected(self) -> None:
        """C：url 长度 >2048 → ValidationError。"""
        with self.assertRaises(ValidationError):
            WebClipRequest(url="https://x.com/" + "a" * 2048, raw_text=_VALID_TEXT)

    # -------------------------------------- D. raw_text 非空（min_length=1）
    def test_d_raw_text_empty_rejected(self) -> None:
        """D：raw_text="" → ValidationError。"""
        with self.assertRaises(ValidationError):
            WebClipRequest(url=_VALID_URL, raw_text="")

    def test_d_raw_text_too_long_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            WebClipRequest(url=_VALID_URL, raw_text="x" * 500_001)

    # ---------------------------------------------- E. title 超长（max_length=512）
    def test_e_title_too_long_rejected(self) -> None:
        """E：title 长度 >512 → ValidationError。"""
        with self.assertRaises(ValidationError):
            WebClipRequest(url=_VALID_URL, raw_text=_VALID_TEXT, title="t" * 513)

    # ---------------------------------------------- F. title 可空
    def test_f_title_none_accepted(self) -> None:
        """F1：title 缺省（None）合法。"""
        req = WebClipRequest(url=_VALID_URL, raw_text=_VALID_TEXT)
        self.assertIsNone(req.title)

    def test_f_title_explicit_none_accepted(self) -> None:
        """F2：title=None 合法。"""
        req = WebClipRequest(url=_VALID_URL, raw_text=_VALID_TEXT, title=None)
        self.assertIsNone(req.title)

    # ---------------------------------------------- G. extra 字段禁止（source_type 不得伪造）
    def test_g_extra_source_type_rejected(self) -> None:
        """G：客户端传入 source_type → ValidationError（extra="forbid"）。"""
        with self.assertRaises(ValidationError):
            WebClipRequest(
                url=_VALID_URL,
                raw_text=_VALID_TEXT,
                source_type="upload",  # type: ignore[call-arg]
            )

    def test_g_extra_unknown_field_rejected(self) -> None:
        """G2：任意未声明字段 → ValidationError。"""
        with self.assertRaises(ValidationError):
            WebClipRequest(  # type: ignore[call-arg]
                url=_VALID_URL,
                raw_text=_VALID_TEXT,
                foo="bar",
            )

    # ---------------------------------------------- I. 合法示例解析
    def test_i_valid_request_parsed(self) -> None:
        """I：合法请求正常解析并保留字段。"""
        req = WebClipRequest(
            url=_VALID_URL,
            title="示例文章",
            raw_text=_VALID_TEXT,
        )
        self.assertEqual(req.url, _VALID_URL)
        self.assertEqual(req.title, "示例文章")
        self.assertEqual(req.raw_text, _VALID_TEXT)
        # 契约：schema 无 source_type 字段（不允许客户端传入）
        self.assertNotIn("source_type", WebClipRequest.model_fields)

    def test_i_url_is_validated_but_preserved_for_service_normalization(self) -> None:
        req = WebClipRequest(
            url="HTTPS://Example.COM:443/article?id=7&utm_source=test#part",
            raw_text=_VALID_TEXT,
        )
        self.assertEqual(
            req.url,
            "HTTPS://Example.COM:443/article?id=7&utm_source=test#part",
        )

    def test_i_non_http_url_and_credentials_rejected(self) -> None:
        for value in (
            "file:///tmp/page.html",
            "https://user:secret@example.com/private",
            "not-a-url",
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                WebClipRequest(url=value, raw_text=_VALID_TEXT)

    # ---------------------------------------------- H. 边界合法值
    def test_h_boundary_lengths_accepted(self) -> None:
        """H：url 恰 2048 / title 恰 512 / raw_text 恰 1 字符均合法。"""
        url_2048 = "https://x.com/" + "a" * (2048 - len("https://x.com/"))
        req = WebClipRequest(
            url=url_2048,
            title="t" * 512,
            raw_text="x",
        )
        self.assertEqual(len(req.url), 2048)
        self.assertEqual(len(req.title), 512)
        self.assertEqual(len(req.raw_text), 1)


class WebClipResponseTest(unittest.TestCase):
    """WebClipResponse 组装契约测试。"""

    def test_a_full_fields_response(self) -> None:
        """A：响应全字段可组装（含网页来源元数据）。"""
        resp = WebClipResponse(
            id=123,
            filename="webclip.txt",
            status="SUCCESS",
            chunk_count=3,
            error_message=None,
            title="示例文章",
            url=_VALID_URL,
            source_type="webpage",
        )
        self.assertEqual(resp.id, 123)
        self.assertEqual(resp.filename, "webclip.txt")
        self.assertEqual(resp.status, "SUCCESS")
        self.assertEqual(resp.chunk_count, 3)
        self.assertIsNone(resp.error_message)
        self.assertEqual(resp.title, "示例文章")
        self.assertEqual(resp.url, _VALID_URL)
        self.assertEqual(resp.source_type, "webpage")

    def test_b_error_message_required_field_present(self) -> None:
        """B：失败态 error_message 可透传。"""
        resp = WebClipResponse(
            id=1,
            filename="webclip.txt",
            status="FAILED",
            chunk_count=0,
            error_message="split failed",
            title=None,
            url=_VALID_URL,
            source_type="webpage",
        )
        self.assertEqual(resp.status, "FAILED")
        self.assertEqual(resp.error_message, "split failed")

    def test_c_source_type_required(self) -> None:
        """C：source_type 必填字段（契约：响应固定返回）。"""
        with self.assertRaises(ValidationError):
            WebClipResponse(  # type: ignore[call-arg]
                id=1,
                filename="webclip.txt",
                status="SUCCESS",
                chunk_count=0,
                error_message=None,
                title=None,
                url=_VALID_URL,
            )


class WebClipApiTest(unittest.TestCase):
    """同步 POST /clips 的 HTTP 级测试（路由装配 + 状态码映射 + 认证边界）。"""

    def setUp(self) -> None:
        """隔离 app + fake WebClipService；不连 MySQL / Milvus / 百炼 / Redis。"""
        self._milvus_init_patcher = patch("backend.main.get_milvus_initializer")
        self.mock_initializer = Mock()
        self.mock_initializer.initialize.return_value = None
        self._milvus_init_patcher.start().return_value = self.mock_initializer

        self.fake_plugin = SimpleNamespace(plugin_id="plugin-42")
        self.fake_plugin_service = Mock()
        self.fake_plugin_service.authenticate.return_value = self.fake_plugin
        self.fake_plugin_service.decrypt_api_key.return_value = "sk-plugin"

        # clip() 是 async，路由里被 await → 必须是 AsyncMock。
        self.fake_web_clip_service = Mock()
        self.fake_web_clip_service.clip = AsyncMock()

        self.app = create_app()
        self.app.dependency_overrides[get_plugin_service] = (
            lambda: self.fake_plugin_service
        )
        self.app.dependency_overrides[get_web_clip_service] = (
            lambda: self.fake_web_clip_service
        )
        # 刻意不覆盖 get_current_plugin：真实依赖在 header 缺失时先抛
        # PluginCredentialsMissingError（deps.py），之后才调 authenticate(Mock)。
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self._milvus_init_patcher.stop()
        self.app.dependency_overrides.clear()

    @staticmethod
    def _headers() -> dict[str, str]:
        return {"X-Plugin-ID": "plugin-42", "X-Plugin-Secret": "secret"}

    @staticmethod
    def _payload(**overrides) -> dict[str, object]:
        payload: dict[str, object] = {
            "url": _VALID_URL,
            "title": "示例文章",
            "raw_text": _VALID_TEXT,
        }
        payload.update(overrides)
        return payload

    def _make_document(self) -> Mock:
        return Mock(
            id=123,
            filename="webclip.txt",
            status="SUCCESS",
            chunk_count=3,
            error_message=None,
            title="示例文章",
            url=_VALID_URL,
            source_type="webpage",
        )

    # ------------------------------------------------------- 1. 201 成功
    def test_clip_success_201(self) -> None:
        """1：正常剪藏 → 201，来源元数据 8 字段原样透传。"""
        self.fake_web_clip_service.clip.return_value = self._make_document()

        response = self.client.post("/clips", json=self._payload(), headers=self._headers())

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.json(),
            {
                "id": 123,
                "filename": "webclip.txt",
                "status": "SUCCESS",
                "chunk_count": 3,
                "error_message": None,
                "title": "示例文章",
                "url": _VALID_URL,
                "source_type": "webpage",
            },
        )
        self.fake_plugin_service.decrypt_api_key.assert_called_once_with(self.fake_plugin)
        self.fake_web_clip_service.clip.assert_called_once_with(
            url=_VALID_URL,
            raw_text=_VALID_TEXT,
            title="示例文章",
            plugin_id="plugin-42",
            api_key="sk-plugin",
        )

    # ------------------------------------- 2. 配额超限 → 429（#24b 写入侧）
    def test_clip_quota_exceeded_429(self) -> None:
        """2：WebClipService 抛 DocumentQuotaExceededError → 429，中文 detail 透传。"""
        self.fake_web_clip_service.clip.side_effect = DocumentQuotaExceededError(
            "知识库文档数已达上限（188 篇），请先删除部分文档后再剪藏"
        )

        response = self.client.post("/clips", json=self._payload(), headers=self._headers())

        self.assertEqual(response.status_code, 429)
        body = response.json()
        self.assertEqual(body["type"], "DocumentQuotaExceededError")
        # 扩展端 parseErrorMessage 直接展示 detail，因此文案必须原样出现在响应里
        self.assertIn("已达上限", body["detail"])

    # --------------------------------------------------- 3. 缺凭证 → 401
    def test_clip_without_credentials_401(self) -> None:
        """3：不带 X-Plugin-ID / X-Plugin-Secret → 401，且不进入剪藏链路。"""
        response = self.client.post("/clips", json=self._payload())

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["type"], "PluginCredentialsMissingError")
        self.fake_web_clip_service.clip.assert_not_called()
        self.fake_plugin_service.decrypt_api_key.assert_not_called()

    # ------------------------------------- 4. 伪造 source_type → 422
    def test_clip_rejects_forged_source_type_422(self) -> None:
        """4：extra="forbid" 在 HTTP 边界同样生效，客户端无法指定来源类型。"""
        response = self.client.post(
            "/clips",
            json=self._payload(source_type="upload"),
            headers=self._headers(),
        )

        self.assertEqual(response.status_code, 422)
        self.fake_web_clip_service.clip.assert_not_called()


if __name__ == "__main__":
    unittest.main()
