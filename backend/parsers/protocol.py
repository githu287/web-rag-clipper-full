"""
DocumentParser 抽象（Protocol）。

契约：parse(file_path) -> str —— 将磁盘文件内容解析为完整文本。
设计要点（与项目 Protocol 风格一致）：
1) Parser 只负责「文件 → str」，不负责 chunk（那是 Chunker 职责）。
2) Parser 禁止调用 Embedding / Milvus / MySQL。
3) 失败抛 DocumentParserError 族（不吞异常，保留根因）。
"""

from __future__ import annotations

from typing import Protocol


class DocumentParser(Protocol):
    """文档解析抽象：文件路径 → 完整文本。"""

    def parse(self, file_path: str) -> str:
        """
        解析文件为完整文本。

        Args:
            file_path: 磁盘上的文件绝对/相对路径。

        Returns:
            文件完整文本（UTF-8 优先解码；空文件返回空字符串）。

        Raises:
            DocumentParserUnsupportedExtensionError: 扩展名不受支持。
            DocumentParserReadError: 文件不存在 / 读取失败 / 编码不兼容。
        """
        ...
