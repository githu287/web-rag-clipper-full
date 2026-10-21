from __future__ import annotations

import unittest

from backend.core.url_normalization import normalize_web_url


class NormalizeWebUrlTest(unittest.TestCase):
    def test_normalizes_scheme_host_default_port_and_empty_path(self) -> None:
        self.assertEqual(
            normalize_web_url(" HTTPS://Example.COM.:443 "),
            "https://example.com/",
        )

    def test_removes_fragment_and_tracking_parameters(self) -> None:
        self.assertEqual(
            normalize_web_url(
                "https://example.com/article?id=7&utm_source=newsletter&FBCLID=x#part"
            ),
            "https://example.com/article?id=7",
        )

    def test_keeps_route_fragment_so_spa_routes_stay_distinct(self) -> None:
        """哈希路由 SPA 的不同页面只在 `#/...` 里：剥掉会让后一次剪藏覆盖上一篇。"""
        self.assertEqual(
            normalize_web_url("https://spa.example/#/users/1"),
            "https://spa.example/#/users/1",
        )
        self.assertEqual(
            normalize_web_url("https://spa.example/#/detail?id=7"),
            "https://spa.example/#/detail?id=7",
        )
        self.assertNotEqual(
            normalize_web_url("https://spa.example/#/users/1"),
            normalize_web_url("https://spa.example/#/users/2"),
        )

    def test_plain_anchor_is_still_stripped(self) -> None:
        """历史剪藏存的是无 fragment 的 URL，锚点必须照旧剥掉才不会重剪变新建。"""
        self.assertEqual(
            normalize_web_url("https://example.com/docs/page#section-2"),
            "https://example.com/docs/page",
        )
        self.assertEqual(
            normalize_web_url("https://example.com/docs/page#"),
            "https://example.com/docs/page",
        )

    def test_legacy_route_prefix_folds_into_canonical_form(self) -> None:
        """`#!/x` 与 `#/x` 是同一条路由的两种写法，必须落到同一个文档键上。"""
        self.assertEqual(
            normalize_web_url("https://spa.example#!/legacy/list"),
            "https://spa.example/#/legacy/list",
        )
        self.assertEqual(
            normalize_web_url("https://spa.example/#!/legacy/list"),
            normalize_web_url("https://spa.example/#/legacy/list"),
        )

    def test_non_ascii_route_fragment_is_percent_encoded_like_browser(self) -> None:
        """扩展端交给后端的是编码后的键，后端必须编成同一串字符才能命中复用。"""
        self.assertEqual(
            normalize_web_url("https://spa.example/#/用户/1"),
            "https://spa.example/#/%E7%94%A8%E6%88%B7/1",
        )

    def test_route_fragment_counts_toward_length_limit(self) -> None:
        """Document.url 列宽 2048：保留路由后超限必须拒绝，不能截断成两个键。"""
        with self.assertRaisesRegex(ValueError, "at most 2048"):
            normalize_web_url("https://spa.example/" + "#" + "/r" * 1100)

    def test_preserves_non_tracking_query_order_and_blank_values(self) -> None:
        self.assertEqual(
            normalize_web_url("https://example.com/search?b=2&a=&b=3"),
            "https://example.com/search?b=2&a=&b=3",
        )

    def test_preserves_non_default_port_and_trailing_slash(self) -> None:
        self.assertEqual(
            normalize_web_url("http://Example.com:8080/docs/"),
            "http://example.com:8080/docs/",
        )

    def test_converts_unicode_hostname_to_idna(self) -> None:
        self.assertEqual(
            normalize_web_url("https://例子.测试/文章"),
            "https://xn--fsqu00a.xn--0zwm56d/%E6%96%87%E7%AB%A0",
        )

    def test_rejects_non_http_scheme(self) -> None:
        with self.assertRaisesRegex(ValueError, "http or https"):
            normalize_web_url("file:///tmp/page.html")

    def test_rejects_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "credentials"):
            normalize_web_url("https://user:secret@example.com/private")

    def test_rejects_missing_or_invalid_host(self) -> None:
        for value in ("https:///missing-host", "not-a-url", "https://example.com:bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_web_url(value)


if __name__ == "__main__":
    unittest.main()
