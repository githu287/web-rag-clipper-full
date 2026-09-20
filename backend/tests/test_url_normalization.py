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
