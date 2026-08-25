"""
Milvus DTO 契约（Phase 2.3 §4 落地）。

经验库 610470 规则应用（Milvus DataNotMatchException 防御）：
  - ChunkVector 字段集 = {id, page_id, chunk_index, chunk_text, embedding}，完全等于
    Phase 2.2 §6 设计的 Milvus Schema 字段集；不多余、不缺失。
  - 所有「必填字段缺失 / 类型错误 / 长度越界 / 不一致」的契约错误都在 Pydantic 构造期
    立即失败，绝不拖到 Milvus 写入期才暴露。
"""

from __future__ import annotations

import math
import re
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 与 Phase 2.2 §7 主键格式：pageId_chunkIndex（应用层确定性生成）
_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d+_\d+$")

# ChunkVector.id 最大长度（Phase 2.2 §6：VARCHAR(64) PK）
_ID_MAX_LENGTH: Final[int] = 64

# chunk_text UTF-8 字节硬上限（Phase 2.2 §9.3：max_length = 4096 字节）
_CHUNK_TEXT_MAX_UTF8_BYTES: Final[int] = 4096

# Embedding 维度：Phase 2.2 §4 锁定 dim=1024；后续若需要修改必须重建 Collection
_EMBEDDING_DIMENSION: Final[int] = 1024

# COSINE 返回值域（保留 Phase 2.2 §11 历史设计的 [0, 2] 校验约束）：
#   - 历史设计（Phase 2.2 §11）将 COSINE 定义为 distance ∈ [0, 2]，0 = 完全相同；
#   - 真实环境（pymilvus 2.4.15）实际返回 COSINE similarity（余弦相似度），
#     自相似返回 1.0，数值越大越相似，真实值域 [-1, 1]；
#   - 因真实相似度恒 ≤ 1.0 < 2.0，le=_DISTANCE_MAX 不会拒绝合法返回值；
#     ge=_DISTANCE_MIN 保留历史设计下界。ChunkSearchResult.distance 实际承载
#     COSINE similarity（详见该字段 description）。
_DISTANCE_MIN: Final[float] = 0.0
_DISTANCE_MAX: Final[float] = 2.0


class _BaseMilvusDto(BaseModel):
    """Milvus DTO 公共基：统一 Pydantic v2 strict 模式 + 禁止额外字段。"""

    model_config = ConfigDict(
        extra="forbid",          # Phase 2.3 契约：DTO 字段必须与 Schema 一致，禁止「额外字段偷偷夹带」
        frozen=False,            # 允许调用方排序/重写；如要不可变可改为 frozen=True
        strict=True,             # 严格类型：不允许 str->int / int->float 静默强转
    )


class ChunkVector(_BaseMilvusDto):
    """
    Milvus 写入用 DTO：与 Phase 2.2 §6 字段 1:1 对齐。

    字段：
        id          : str       PK；格式 "{page_id}_{chunk_index}"；长度 ≤ 64
        page_id     : int       对应 Document.id（当前实现 document.id = Milvus.page_id 1:1，Phase 2.12 Step 4）；≥ 0
        chunk_index : int       page 内从 0 递增；≥ 0
        chunk_text  : str       非空；UTF-8 字节长度 ≤ 4096
        embedding   : list[float]  长度必须 == 1024；所有元素 finite（非 NaN/非 Inf）
    """

    id: str = Field(
        ...,
        min_length=1,
        max_length=_ID_MAX_LENGTH,
        description="主键：应用层确定性生成 f\"{page_id}_{chunk_index}\"（VARCHAR(64)，Phase 2.2 §7）",
    )
    page_id: int = Field(
        ...,
        ge=0,
        description="对应 Document.id（当前 document.id = Milvus.page_id 1:1，不再存在 pages 表）；非负（Milvus 以 INT64 承载，无 UNSIGNED）",
    )
    chunk_index: int = Field(
        ...,
        ge=0,
        description="page 内 chunk 序号，从 0 起单调递增",
    )
    chunk_text: str = Field(
        ...,
        min_length=1,
        description="chunk 原文（Phase 2.2 §9.3：UTF-8 字节 ≤ 4096）",
    )
    embedding: list[float] = Field(
        ...,
        min_length=_EMBEDDING_DIMENSION,
        max_length=_EMBEDDING_DIMENSION,
        description="百炼 text-embedding-v3 输出向量；dim 必须与 Milvus embedding.dim=1024 严格一致",
    )

    # ------------------------------------------------------------------ 字段级校验
    @field_validator("id")
    @classmethod
    def _validate_id_pattern(cls, v: str) -> str:
        """id 必须严格符合 {page_id}_{chunk_index} 数字格式。"""
        if not _ID_PATTERN.fullmatch(v):
            raise ValueError(
                f"ChunkVector.id 格式非法：{v!r}，必须匹配正则 ^\\d+_\\d+$（如 \"100_0\"）"
            )
        return v

    @field_validator("chunk_text")
    @classmethod
    def _validate_chunk_text_utf8_bytes(cls, v: str) -> str:
        """Phase 2.2 §9.3：chunk_text VARCHAR(4096) 是 UTF-8 字节限制，不是字符数。"""
        byte_len = len(v.encode("utf-8"))
        if byte_len > _CHUNK_TEXT_MAX_UTF8_BYTES:
            raise ValueError(
                f"ChunkVector.chunk_text UTF-8 字节长度 {byte_len} > {_CHUNK_TEXT_MAX_UTF8_BYTES}"
                "（Milvus VARCHAR 按字节计数；请在 Service 层拆分/截断 chunk）"
            )
        return v

    @field_validator("embedding")
    @classmethod
    def _validate_embedding_finite(cls, v: list[float]) -> list[float]:
        """维度由 Field min/max_length 强制=1024；本校验保证向量元素无 NaN/Inf。"""
        for idx, value in enumerate(v):
            # math.isfinite 同时覆盖 NaN / +Inf / -Inf；bool 是 int 子类，True/False 也会被接受，
            # 但 embedding 通常来自百炼 API 的 float 列表，严格模式下 bool 已被 strict=True 挡住。
            if not math.isfinite(float(value)):
                raise ValueError(
                    f"ChunkVector.embedding[{idx}] = {value!r} 非法（必须是有限实数；不能是 NaN/Inf）"
                )
        return v

    # ------------------------------------------------------------------ 模型级校验
    @model_validator(mode="after")
    def _validate_id_consistent_with_page_id_and_chunk_index(self) -> "ChunkVector":
        """
        经验库 610470「字段契约一致性」防御：
        避免 Service 层拼错 id，导致 upsert 覆盖到错误 page 的 chunk。
        要求 id 字符串必须严格等于 f"{page_id}_{chunk_index}"。
        """
        expected = f"{self.page_id}_{self.chunk_index}"
        if self.id != expected:
            raise ValueError(
                f"ChunkVector.id={self.id!r} 与 page_id/chunk_index 不一致：期望 {expected!r}"
            )
        return self


class ChunkSearchResult(_BaseMilvusDto):
    """
    Milvus 检索用 DTO（Phase 2.3 §4.2；不含 embedding）。

    字段：
        id          : str       Milvus PK（调试/日志）
        page_id     : int       反查 Document（id/title/status 等）构造 citation 的桥梁（Document.id = Milvus.page_id 1:1）
        chunk_index : int       引用排序/调试
        chunk_text  : str       LLM context 拼接原文
        distance    : float     COSINE similarity（余弦相似度）∈ [-1,1]；越大越相似，1.0 = 完全相似
                                （真实 pymilvus 2.4.15 行为，见 distance 字段 description）

    红线（Phase 2.2 §13.1）：DTO 定义中严禁包含 embedding 字段；
    Repository Impl output_fields 不得包含 embedding，避免带宽浪费。
    """

    id: str = Field(
        ...,
        min_length=1,
        max_length=_ID_MAX_LENGTH,
        description="Milvus chunk PK id（调试/日志用途）",
    )
    page_id: int = Field(
        ...,
        ge=0,
        description="对应 Document.id（当前 document.id = Milvus.page_id 1:1）；用于反查 citation",
    )
    chunk_index: int = Field(
        ...,
        ge=0,
        description="page 内 chunk 序号；用于 citation 排序",
    )
    chunk_text: str = Field(
        ...,
        min_length=1,
        description="chunk 原文（用于 LLM context 拼接）",
    )
    distance: float = Field(
        ...,
        ge=_DISTANCE_MIN,
        le=_DISTANCE_MAX,
        description="Milvus COSINE similarity（余弦相似度）；数值越大越相似，1.0 = 完全相似；"
        "真实值域 [-1,1]（保留 Phase 2.2 §11 历史设计的 [0,2] 校验约束）",
    )
