"""
Web Clip API Schema 单元测试（Phase 3.1 Step 3）。

覆盖用例（对应 Step 3 §十 测试要求 8~9 及契约）：
  A. WebClipRequest：url / raw_text 必填（缺字段 → ValidationError）。
  B. url 空字符串 → ValidationError（min_length=1）。
  C. url 超长（>2048）→ ValidationError（max_length=2048）。
  D. raw_text 空字符串 → ValidationError（min_length=1）。
  E. title 超长（>512）→ ValidationError（max_length=512）。
  F. title=None / 缺省 → 合法。
  G. extra 字段（如 source_type）→ ValidationError（extra="forbid"，
     禁止客户端伪造 source_type，与 WebClip 契约一致）。
  H. WebClipResponse 全字段组装（12 字段契约校验）。
  I. WebClipRequest 合法示例正常解析。

技术栈：Pydantic v2 模型直接校验（无需 TestClient / 数据库）。
"""

from __future__ import annotations

import unittest

from pydantic import ValidationError

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
        self.assertNotIn("source_type", req.model_fields)

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


if __name__ == "__main__":
    unittest.main()
