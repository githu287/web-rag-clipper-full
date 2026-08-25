"""
文本切分层（Phase 2.10 Step 2）。

提供：
    - Chunker：切分抽象（Protocol），定义 split(text) -> list[str] 契约。
    - RecursiveCharacterChunker：递归字符切分实现（chunk_size / chunk_overlap）。

分层边界：
    - 本包只负责「完整文本 str → list[str]」，不解析文件（那是 parsers 职责）；
    - 禁止调用 Embedding / Milvus / MySQL；
    - 不依赖 LangChain（自实现递归字符切分，避免引入额外框架）。
"""

from __future__ import annotations

from .protocol import Chunker
from .recursive import RecursiveCharacterChunker

__all__ = ["Chunker", "RecursiveCharacterChunker"]
