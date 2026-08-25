"""
Chunker 抽象（Protocol）。

契约：split(text) -> list[str] —— 将完整文本切分为有序块列表。
设计要点（与项目 Protocol 风格一致）：
1) Chunker 只负责「str → list[str]」，不解析文件（那是 Parser 职责）。
2) Chunker 禁止调用 Embedding / Milvus / MySQL。
3) 输入空文本返回 []；chunk 非空；顺序保持原文顺序。
4) 配置非法（chunk_overlap >= chunk_size 等）抛 DocumentChunkingError 族。
"""

from __future__ import annotations

from typing import Protocol


class Chunker(Protocol):
    """文本切分抽象：完整文本 → 有序 chunk 列表。"""

    def split(self, text: str) -> list[str]:
        """
        将完整文本切分为有序 chunk 列表。

        Args:
            text: 完整文本（空字符串合法，返回 []）。

        Returns:
            按原文顺序排列的非空 chunk 列表；空文本返回 []。

        Raises:
            DocumentChunkingConfigError: 配置非法（chunk_size/chunk_overlap 约束不满足）。
            DocumentChunkingError: 切分过程中不可恢复的内部错误。
        """
        ...
