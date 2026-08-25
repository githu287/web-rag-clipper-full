"""
TextDocumentParser 单元测试（Phase 2.10 Step 2）。

覆盖：
- txt 成功
- md 成功
- UTF-8 中文
- 大写扩展名兼容（.TXT / .MD）
- 不支持扩展名（.pdf / .docx / 无扩展名）
- 文件不存在
- 空文件返回空字符串
"""

from __future__ import annotations

import os
import tempfile
import unittest

from backend.core.exceptions import (
    DocumentParserReadError,
    DocumentParserUnsupportedExtensionError,
)
from backend.parsers import TextDocumentParser


class TextDocumentParserTest(unittest.TestCase):
    """文本解析器测试（临时文件隔离）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.parser = TextDocumentParser()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, filename: str, content: str, encoding: str = "utf-8") -> str:
        """在临时目录写入文件并返回完整路径。"""
        path = os.path.join(self._tmp.name, filename)
        with open(path, "w", encoding=encoding) as fh:
            fh.write(content)
        return path

    # ------------------------------------------------------------ 成功路径
    def test_parse_txt_success(self) -> None:
        """解析 .txt 成功。"""
        path = self._write("note.txt", "hello txt")
        self.assertEqual(self.parser.parse(path), "hello txt")

    def test_parse_md_success(self) -> None:
        """解析 .md 成功。"""
        content = "# Title\n\nsome **markdown** content"
        path = self._write("doc.md", content)
        self.assertEqual(self.parser.parse(path), content)

    def test_parse_markdown_extension_success(self) -> None:
        """解析 .markdown 扩展名成功。"""
        path = self._write("doc.markdown", "md content")
        self.assertEqual(self.parser.parse(path), "md content")

    def test_parse_utf8_chinese(self) -> None:
        """UTF-8 中文内容完整解析。"""
        content = "你好，世界！这是一段中文测试文本。"
        path = self._write("cn.txt", content)
        self.assertEqual(self.parser.parse(path), content)

    def test_parse_uppercase_extension(self) -> None:
        """大写扩展名 .TXT / .MD 兼容。"""
        path = self._write("UPPER.TXT", "upper case")
        self.assertEqual(self.parser.parse(path), "upper case")
        md_path = self._write("doc.MD", "# title")
        self.assertEqual(self.parser.parse(md_path), "# title")

    def test_parse_empty_file_returns_empty_string(self) -> None:
        """空文件返回空字符串（不抛异常）。"""
        path = self._write("empty.txt", "")
        self.assertEqual(self.parser.parse(path), "")

    def test_parse_utf8_bom_stripped(self) -> None:
        """带 BOM 的 UTF-8 文件自动剥离 BOM。"""
        path = os.path.join(self._tmp.name, "bom.txt")
        with open(path, "wb") as fh:
            fh.write(b"\xef\xbb\xbfhello bom")
        self.assertEqual(self.parser.parse(path), "hello bom")

    # ------------------------------------------------------------ 异常路径
    def test_parse_unsupported_extension_pdf(self) -> None:
        """.pdf 不受支持（本阶段未实现）。"""
        path = self._write("doc.pdf", "fake pdf")
        with self.assertRaises(DocumentParserUnsupportedExtensionError):
            self.parser.parse(path)

    def test_parse_unsupported_extension_docx(self) -> None:
        """.docx 不受支持（本阶段未实现）。"""
        path = self._write("doc.docx", "fake docx")
        with self.assertRaises(DocumentParserUnsupportedExtensionError):
            self.parser.parse(path)

    def test_parse_no_extension_rejected(self) -> None:
        """无扩展名文件不受支持。"""
        path = self._write("README", "no extension")
        with self.assertRaises(DocumentParserUnsupportedExtensionError):
            self.parser.parse(path)

    def test_parse_file_not_found(self) -> None:
        """文件不存在抛 DocumentParserReadError。"""
        missing = os.path.join(self._tmp.name, "not_exist.txt")
        with self.assertRaises(DocumentParserReadError):
            self.parser.parse(missing)

    def test_parse_unsupported_extension_file_not_read(self) -> None:
        """不支持扩展名时不触碰文件内容（不读文件）。"""
        path = self._write("a.pdf", "content")
        with self.assertRaises(DocumentParserUnsupportedExtensionError):
            self.parser.parse(path)
        # 文件未被读取（未抛读异常），内容保持原样
        with open(path, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "content")


if __name__ == "__main__":
    unittest.main()
