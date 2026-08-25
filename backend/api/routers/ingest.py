"""
Ingest Router：页面 chunk 入库 HTTP 接口（Phase 2.7 Step 2）。

路由：
    POST /ingest/page

依赖注入：
    通过 FastAPI Depends(get_ingest_service) 获取 IngestService 实例。

职责边界：
    - 接收 IngestRequest + Pydantic 校验；
    - 调用 IngestService.ingest_page()；
    - 返回 IngestResponse；
    - 不直接接触 EmbeddingClient / MilvusRepository / pymilvus / openai。

异常处理：
    Service 抛出的 MilvusRepositoryError / EmbeddingClientError 由 main.py 全局
    exception_handler 统一转换为 HTTPException；本 Router 不在路由内 try/except 吞异常。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from ...core.di import get_ingest_service
from ...models.api_schema import IngestRequest, IngestResponse
from ...services.ingest import IngestService

# 创建 Router（prefix + tags 严格按 Phase 2.7 Step 2 要求）
router: APIRouter = APIRouter(
    prefix="/ingest",
    tags=["ingest"],
)


@router.post(
    "/page",
    response_model=IngestResponse,
    status_code=status.HTTP_200_OK,
    summary="页面 chunk 入库",
    description="将已切分的 chunk 文本列表入库 Milvus（re-ingest 三步流程：query old → upsert new → delete stale）",
)
async def ingest_page(
    request: IngestRequest,
    service: IngestService = Depends(get_ingest_service),
) -> IngestResponse:
    """
    POST /ingest/page

    业务流程（Phase 2.2 §15 re-ingest）：
        1) service.ingest_page(page_id, chunks) 内部执行：
           - query old_ids
           - embedding + 构造 ChunkVector + upsert
           - delete stale_ids（差集）

    幂等性：
        相同 page_id + 相同 chunks 多次调用 → 结果一致（upsert 按 PK 覆盖 + delete 差集）。

    Args:
        request: IngestRequest（page_id > 0；chunks 非空）。
        service: 通过 DI 注入的 IngestService 实例。

    Returns:
        IngestResponse: success=True + message="success"。

    Raises:
        MilvusRepositoryError: Milvus 操作失败（由全局 handler 转 HTTPException）。
        EmbeddingClientError: 百炼 Embedding 调用失败（由全局 handler 转 HTTPException）。
    """
    await service.ingest_page(
        page_id=request.page_id,
        chunks=request.chunks,
    )
    return IngestResponse()
