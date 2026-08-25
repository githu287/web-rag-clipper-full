"""
TextDocumentParser：TXT / Markdown 文本解析实现（Phase 2.10 Step 2）。

支持扩展名：.txt / .md / .markdown（大小写不敏感）。
编码策略（UTF-8 优先）：
1) 优先 UTF-8-sig（兼容带 BOM 的 UTF-8 文件，BOM 会被自动剥离）；
2) UTF-8 解码失败时回退 GBK（中文 Windows 常见编码）；
3) 回退仍失败 → DocumentParserReadError（保留根因）。

分层边界：
- 只负责「文件 → str」，不切分（Chunker 职责）；
- 禁止调用 Embedding / Milvus / MySQL；
- 本阶段不实现 PDF / DOCX（不引入额外依赖）。
"""

from __future__ import annotations

import logging
import os
from typing import Final

from ..core.exceptions import (
    DocumentParserReadError,
    DocumentParserUnsupportedExtensionError,
)

logger: logging.Logger = logging.getLogger(__name__)

_SUPPORTED_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".txt", ".md", ".markdown"}
)
# 尝试顺序：UTF-8 优先；GBK 兜底（中文 Windows 常见）
_READ_ENCODINGS: Final[tuple[str, ...]] = ("utf-8-sig", "gbk")


class TextDocumentParser:
    """文本文件解析器（txt / md / markdown）。"""

    def parse(self, file_path: str) -> str:
        """
        解析文本文件为完整字符串。

        Args:
            file_path: 磁盘文件路径。

        Returns:
            文件文本内容（UTF-8 优先；空文件返回空字符串）。

        Raises:
            DocumentParserUnsupportedExtensionError: 扩展名不在支持集内。
            DocumentParserReadError: 文件不存在 / 不可读 / 编码不兼容。
        """
        extension = os.path.splitext(file_path)[1].lower()
        if extension not in _SUPPORTED_EXTENSIONS:
            raise DocumentParserUnsupportedExtensionError(
                f"不支持的文档扩展名: {extension or '(无扩展名)'} "
                f"（支持: {', '.join(sorted(_SUPPORTED_EXTENSIONS))}）"
            )

        return self._read_text(file_path)

    # ------------------------------------------------------------------ 内部
    def _read_text(self, file_path: str) -> str:
        """按 UTF-8 → GBK 顺序读取文件，全部失败抛 DocumentParserReadError。"""
        last_exc: Exception | None = None
        for encoding in _READ_ENCODINGS:
            try:
                with open(file_path, "r", encoding=encoding) as fh:
                    return fh.read()
            except UnicodeDecodeError as exc:
                last_exc = exc
                logger.debug(
                    "TextDocumentParser: 编码 %s 解码失败，尝试下一编码: %s",
                    encoding,
                    file_path,
                )
            except FileNotFoundError as exc:
                # 文件不存在：无需再尝试其他编码，直接抛明确异常
                raise DocumentParserReadError(
                    f"文件不存在: {file_path}"
                ) from exc
            except OSError as exc:
                # 权限 / IO 错误：同样无编码回退意义
                raise DocumentParserReadError(
                    f"读取文件失败: {file_path}（{exc}）"
                ) from exc

        # 全部编码尝试失败（last_exc 必为 UnicodeDecodeError）
        raise DocumentParserReadError(
            f"文件编码不兼容: {file_path}（尝试 {_READ_ENCODINGS} 均失败）"
        ) from last_exc
