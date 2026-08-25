"""
RecursiveCharacterChunker 单元测试（Phase 2.10 Step 2）。

覆盖：
- 普通短文本单块还原原文
- 空文本返回 []
- chunk_size 生效（每块不超上限）
- chunk_overlap 生效（相邻块首尾重叠）
- chunk 顺序保持原文
- 无重复（原文 marker 每个恰好出现一次；overlap=0 时拼接可还原原文）
- 超长文本切分为多块
- 非法配置（chunk_size<1 / overlap<0 / overlap>=size）抛 DocumentChunkingConfigError
"""

from __future__ import annotations

import unittest

from backend.chunkers import RecursiveCharacterChunker
from backend.core.exceptions import DocumentChunkingConfigError


class RecursiveCharacterChunkerTest(unittest.TestCase):
    """递归字符切分器测试。"""

    # ------------------------------------------------------------ 基本行为
    def test_short_text_returns_single_chunk(self) -> None:
        """短文本（<= chunk_size）单块且内容等于原文。"""
        text = "hello world, this is a short document."
        chunks = RecursiveCharacterChunker().split(text)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], text)

    def test_empty_text_returns_empty_list(self) -> None:
        """空文本返回 []。"""
        self.assertEqual(RecursiveCharacterChunker().split(""), [])

    # ------------------------------------------------------------ chunk_size
    def test_chunk_size_respected(self) -> None:
        """每块长度不超过 chunk_size。"""
        text = "a" * 2000
        chunks = RecursiveCharacterChunker(chunk_size=100, chunk_overlap=0).split(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 100)

    def test_chunk_size_custom(self) -> None:
        """自定义 chunk_size 生效（不同 size 产生不同块数）。"""
        text = "段落文本" * 100
        small = RecursiveCharacterChunker(chunk_size=20, chunk_overlap=0).split(text)
        large = RecursiveCharacterChunker(chunk_size=100, chunk_overlap=0).split(text)
        self.assertGreater(len(small), len(large))

    # ------------------------------------------------------------ overlap
    def test_overlap_applied_between_adjacent_chunks(self) -> None:
        """相邻 chunk 首尾保留 chunk_overlap 重叠。"""
        text = "x" * 1000
        overlap = 20
        chunks = RecursiveCharacterChunker(
            chunk_size=100, chunk_overlap=overlap
        ).split(text)
        self.assertGreater(len(chunks), 1)
        for prev, nxt in zip(chunks, chunks[1:]):
            self.assertTrue(nxt.startswith(prev[-overlap:]))

    # ------------------------------------------------------------ 顺序 & 无重复
    def test_chunks_preserve_order(self) -> None:
        """chunk 内容保持原文顺序。"""
        paragraphs = [f"para-{i:02d}" for i in range(30)]
        text = "\n\n".join(paragraphs)
        chunks = RecursiveCharacterChunker(
            chunk_size=50, chunk_overlap=5
        ).split(text)

        flat = "".join(chunks)
        positions = [flat.find(marker) for marker in paragraphs]
        self.assertEqual(positions, sorted(positions))
        self.assertNotEqual(positions[0], -1)

    def test_no_duplicate_chunks(self) -> None:
        """原文 marker 每个恰好出现一次（无重复 chunk 内容）。"""
        paragraphs = [f"para-{i:02d}" for i in range(30)]
        text = "\n\n".join(paragraphs)
        chunks = RecursiveCharacterChunker(
            chunk_size=50, chunk_overlap=5
        ).split(text)

        flat = "".join(chunks)
        for marker in paragraphs:
            self.assertEqual(flat.count(marker), 1)

    def test_zero_overlap_reconstructs_original(self) -> None:
        """chunk_overlap=0 时所有 chunk 拼接等于原文（无遗漏、无新增）。"""
        text = "\n\n".join(f"p{i}" for i in range(60))
        chunks = RecursiveCharacterChunker(
            chunk_size=30, chunk_overlap=0
        ).split(text)
        self.assertEqual("".join(chunks), text)

    # ------------------------------------------------------------ 超长文本
    def test_super_long_text_split_into_multiple_chunks(self) -> None:
        """2000 字符超长文本被切分为多块。"""
        text = "这是一个超长文本。" * 100  # 900 字符
        chunks = RecursiveCharacterChunker().split(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk for chunk in chunks))  # 所有 chunk 非空

    # ------------------------------------------------------------ 非法配置
    def test_chunk_size_zero_rejected(self) -> None:
        """chunk_size < 1 抛 DocumentChunkingConfigError。"""
        with self.assertRaises(DocumentChunkingConfigError):
            RecursiveCharacterChunker(chunk_size=0, chunk_overlap=0)

    def test_chunk_overlap_negative_rejected(self) -> None:
        """chunk_overlap < 0 抛 DocumentChunkingConfigError。"""
        with self.assertRaises(DocumentChunkingConfigError):
            RecursiveCharacterChunker(chunk_size=10, chunk_overlap=-1)

    def test_chunk_overlap_gte_chunk_size_rejected(self) -> None:
        """chunk_overlap >= chunk_size 抛 DocumentChunkingConfigError。"""
        with self.assertRaises(DocumentChunkingConfigError):
            RecursiveCharacterChunker(chunk_size=700, chunk_overlap=700)
        with self.assertRaises(DocumentChunkingConfigError):
            RecursiveCharacterChunker(chunk_size=10, chunk_overlap=15)

    def test_chunk_overlap_less_than_size_allowed(self) -> None:
        """chunk_overlap < chunk_size 边界合法（如 0 / 699 / 700）。"""
        RecursiveCharacterChunker(chunk_size=700, chunk_overlap=0)
        RecursiveCharacterChunker(chunk_size=700, chunk_overlap=699)


if __name__ == "__main__":
    unittest.main()
