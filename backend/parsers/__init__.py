"""
文档解析层（Phase 2.10 Step 2）。

提供：
    - DocumentParser：解析抽象（Protocol），定义 parse(file_path) -> str 契约。
    - TextDocumentParser：文本解析实现，支持 .txt / .md / .markdown（UTF-8 优先）。

分层边界：
    - 本包只负责「文件 → 完整文本 str」，不负责切分（那是 chunkers 的职责）；
    - 禁止调用 Embedding / Milvus / MySQL；
    - 本阶段不实现 PDF / DOCX（不引入 pypdf / python-docx 依赖）。
"""

from __future__ import annotations

from .protocol import DocumentParser
from .text import TextDocumentParser

__all__ = ["DocumentParser", "TextDocumentParser"]
