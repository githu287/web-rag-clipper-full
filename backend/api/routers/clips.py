"""
Web Clip Router（Phase 3.1 Step 3）。

路由：
    POST /clips   → 网页剪藏后端接收：Document(PENDING) → PROCESSING → Chunker
                  → DocumentIngestService → SUCCESS，返回来源元数据（201）

依赖注入（通过 DI 工厂，不直接实例化任何 Repository / Service）：
    - Depends(get_current_plugin)  → PluginWorkspace（X-Plugin-ID + X-Plugin-Secret，Phase 3.5 Step 2-D）
    - Depends(get_web_clip_service)→ WebClipService
    - Depends(get_plugin_service)  → PluginService（解密插件工作空间 API Key）

职责边界：
    - 接收 Pydantic Schema + 校验（WebClipRequest：url / title / raw_text）；
    - 解析当前插件工作空间（get_current_plugin；凭证缺失 → 401）；
    - 解密当前插件工作空间的百炼 API Key（PluginService.decrypt_api_key），注入 WebClipService；
    - 调用 WebClipService.clip() 执行完整网页剪藏链路
      （内部 create_document(PENDING) → update_status(PROCESSING) → Chunker.split
       → DocumentIngestService.ingest_document → SUCCESS/FAILED）；
    - 构造 WebClipResponse 返回（201）；
    - 不接触 EmbeddingClient / MilvusRepository / pymilvus / openai / SQLAlchemy Session。

source_type 不允许客户端传入（WebClipRequest extra="forbid"）：
后端固定为 "webpage"；WebClip 不创建物理文件（file_path=""），
filename 固定 "webclip.txt"。

异常处理：
    - WebClipRequest 校验失败由 FastAPI 自动转 422；
    - DocumentChunkingError / DocumentNotFoundError / DocumentOperationError /
      MilvusRepositoryError / EmbeddingClientError 由 main.py 全局 handler 转
      500 / 404 / 503 / 503 / 502；
    - 本 Router 不 try/except 吞异常。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from ...core.di import get_plugin_service, get_web_clip_service
from ...models import PluginWorkspace
from ...models.document_api_schema import WebClipRequest, WebClipResponse
from ...services.plugin_service import PluginService
from ...services.web_clip import WebClipService
from ..deps import get_current_plugin

# 创建 Router（prefix + tags 与既有 documents/ingest/rag Router 风格一致）
router: APIRouter = APIRouter(
    prefix="/clips",
    tags=["clips"],
)


@router.post(
    "",
    response_model=WebClipResponse,
    status_code=status.HTTP_201_CREATED,
    summary="网页剪藏入库",
    description=(
        "接收未来 Browser Extension 发送的网页 URL / 标题 / 正文纯文本，"
        "串起完整链路：create_document(PENDING) → update_status(PROCESSING) "
        "→ Chunker.split() → DocumentIngestService.ingest_document() → SUCCESS。"
        "source_type 固定为 webpage；不创建物理文件（file_path=\"\"）；"
        "filename 固定为 webclip.txt。需 X-Plugin-ID + X-Plugin-Secret 凭证"
        "（Phase 3.5 Step 2-D）。"
    ),
)
async def create_clip(
    request: WebClipRequest,
    current_plugin: PluginWorkspace = Depends(get_current_plugin),
    web_clip_service: WebClipService = Depends(get_web_clip_service),
    plugin_service: PluginService = Depends(get_plugin_service),
) -> WebClipResponse:
    """
    POST /clips

    业务流程：
        1) request 已由 Pydantic 校验（url / raw_text 非空、title 可空）；
        2) current_plugin 由 X-Plugin-ID + X-Plugin-Secret 解析
           （get_current_plugin；凭证缺失 → 401）；
        3) plugin_service.decrypt_api_key(current_plugin) 解密当前插件工作空间
           的百炼 API Key（Phase 3.5 Step 2-C；明文仅存在于调用栈内存）；
        4) web_clip_service.clip(url, raw_text, title,
           plugin_id=current_plugin.plugin_id, api_key=api_key) 执行完整网页
           剪藏链路（见 services/web_clip.py docstring）；
        5) 成功后构造 WebClipResponse 返回（201）。

    Args:
        request: WebClipRequest（url / title / raw_text）。
        current_plugin: 当前插件工作空间（X-Plugin-ID + X-Plugin-Secret
            → PluginWorkspace；Phase 3.5 Step 2-D）。
        web_clip_service: 通过 DI 注入的 WebClipService 实例。
        plugin_service: 通过 DI 注入的 PluginService（解密插件工作空间的 API Key）。

    Returns:
        WebClipResponse: id / filename="webclip.txt" / status=SUCCESS /
        chunk_count / error_message=None / title / url / source_type="webpage"。

    Raises:
        PluginCredentialsMissingError / PluginNotFoundError / PluginSecretMismatchError:
            凭证缺失 / 无效（401，get_current_plugin 抛出）。
        PluginDisabledError: 插件工作空间被禁用（403）。
        SecurityDecryptionError: API Key 解密失败（500）。
        DocumentChunkingError: Chunker 切分失败（500，Document 已置 FAILED）。
        DocumentRepositoryError / MilvusRepositoryError / EmbeddingClientError:
            链路失败（503 / 502）。
    """
    api_key = plugin_service.decrypt_api_key(current_plugin)
    document = await web_clip_service.clip(
        url=request.url,
        raw_text=request.raw_text,
        title=request.title,
        plugin_id=current_plugin.plugin_id,
        api_key=api_key,
    )
    return WebClipResponse(
        id=document.id,
        filename=document.filename,
        status=document.status,
        chunk_count=document.chunk_count,
        error_message=document.error_message,
        title=document.title,
        url=document.url,
        source_type=document.source_type,
    )
