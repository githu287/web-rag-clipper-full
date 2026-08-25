"""
RecursiveCharacterChunker：递归字符切分实现（Phase 2.10 Step 2）。

算法（自实现，不依赖 LangChain；语义对齐 RecursiveCharacterTextSplitter）：
1) 按分隔符优先级逐层递归：优先按段落（\\n\\n）、再按换行、再按中文句读
   （。！？；，、）、再按空格，最后退化为逐字符。
2) 每层选出「在文本中出现」的最高优先级分隔符切分；片段小于 chunk_size 的
   暂存，超过的继续用更细分隔符递归。
3) 最终用 chunk_size 合并片段，相邻 chunk 之间保留 chunk_overlap 字符重叠，
   保证上下文不丢失且无重复内容遗漏。

设计约束：
- chunk 非空、顺序保持原文、不产生非原文的重复内容；
- 空文本返回 []；
- 配置非法（chunk_size < 1 / chunk_overlap < 0 / chunk_overlap >= chunk_size）
  在 __init__ 抛 DocumentChunkingConfigError；
- 超过 max_page_content_bytes 的文本不进本类（由上层 Upload Service 提前拒绝）。
"""

from __future__ import annotations

from typing import Final

from ..core.exceptions import DocumentChunkingConfigError

# 分隔符优先级（从粗到细）；最后空串表示逐字符退化
_DEFAULT_SEPARATORS: Final[tuple[str, ...]] = (
    "\n\n",
    "\n",
    "。",
    "！",
    "？",
    "；",
    "，",
    "、",
    " ",
    "",
)


class RecursiveCharacterChunker:
    """递归字符切分器。"""

    def __init__(self, chunk_size: int = 700, chunk_overlap: int = 100) -> None:
        """
        Args:
            chunk_size: 单块最大字符数（>= 1）。
            chunk_overlap: 相邻块重叠字符数（0 <= overlap < chunk_size）。

        Raises:
            DocumentChunkingConfigError: 配置不满足上述约束。
        """
        if chunk_size < 1:
            raise DocumentChunkingConfigError(
                f"chunk_size 必须 >= 1，当前: {chunk_size}"
            )
        if chunk_overlap < 0:
            raise DocumentChunkingConfigError(
                f"chunk_overlap 必须 >= 0，当前: {chunk_overlap}"
            )
        if chunk_overlap >= chunk_size:
            raise DocumentChunkingConfigError(
                f"chunk_overlap({chunk_overlap}) 必须小于 chunk_size({chunk_size})"
            )
        self._chunk_size: int = chunk_size
        self._chunk_overlap: int = chunk_overlap

    # ---------------------------------------------------------------- 对外 API
    def split(self, text: str) -> list[str]:
        """
        将完整文本切分为有序非空 chunk 列表。

        Args:
            text: 完整文本；空字符串返回 [].

        Returns:
            非空 chunk 列表（保持原文顺序）。
        """
        if not text:
            return []
        splits = self._split_text(text, list(_DEFAULT_SEPARATORS))
        return [s for s in splits if s]

    # ------------------------------------------------------------------ 内部
    def _split_text(self, text: str, separators: list[str]) -> list[str]:
        """
        按当前分隔符集递归切分，返回未合并的片段列表。

        逻辑（对齐 langchain RecursiveCharacterTextSplitter._split_text）：
        - 选「在 text 中出现」的最高优先级分隔符；
        - 按其切分；短片段暂存（good），长片段继续用更细分隔符递归；
        - 若已无可细分分隔符，长片段原样保留（后续 merge 阶段硬切）。
        """
        final_chunks: list[str] = []
        separator = separators[-1]
        new_separators: list[str] = []
        for i, sep in enumerate(separators):
            if sep == "":
                separator = ""
                break
            if sep in text:
                separator = sep
                new_separators = separators[i + 1 :]
                break

        parts = self._split_parts(text, separator)

        good: list[str] = []
        for part in parts:
            if len(part) < self._chunk_size:
                good.append(part)
            else:
                if good:
                    final_chunks.extend(self._merge_splits(good))
                    good = []
                if not new_separators:
                    final_chunks.append(part)
                else:
                    final_chunks.extend(self._split_text(part, new_separators))
        if good:
            final_chunks.extend(self._merge_splits(good))
        return final_chunks

    @staticmethod
    def _split_parts(text: str, separator: str) -> list[str]:
        """
        按 separator 切分并**保留分隔符**为独立片段（keep-separator 策略）。

        保留分隔符的目的：合并时直接拼接即可还原原文（无遗漏、无新增），
        且 chunk 边界上下文不丢失。空串片段被过滤。
        """
        if not separator:
            return list(text)
        parts: list[str] = []
        for i, piece in enumerate(text.split(separator)):
            if i > 0:
                parts.append(separator)
            if piece:
                parts.append(piece)
        return parts

    def _merge_splits(self, splits: list[str]) -> list[str]:
        """
        将片段（含保留的分隔符片段）直接拼接为不超过 chunk_size 的块，
        相邻块间保留 chunk_overlap 字符重叠。

        重叠实现：新块以「上一块末尾 chunk_overlap 个字符」开头，保证边界
        上下文不丢失；overlap=0 时所有块拼接即还原原文。
        """
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for split in splits:
            if current and current_len + len(split) > self._chunk_size:
                chunk_text = "".join(current)
                chunks.append(chunk_text)

                if self._chunk_overlap > 0:
                    overlap_text = chunk_text[-self._chunk_overlap :]
                    current = [overlap_text]
                    current_len = len(overlap_text)
                else:
                    current = []
                    current_len = 0

            current.append(split)
            current_len += len(split)

        if current:
            chunks.append("".join(current))
        return chunks
