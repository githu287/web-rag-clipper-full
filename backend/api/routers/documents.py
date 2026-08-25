"""
Document Router：Document 生命周期 HTTP 接口（Phase 2.9 Step 3；Phase 2.11 Step 2 增 DELETE）。

路由：
    POST /documents                        → 创建 Document（status=PENDING, chunk_count=0）
    POST /documents/upload                 → multipart 上传文件并完整入库（Phase 2.10 Step 3）
    POST /documents/{document_id}/ingest   → Document 生命周期 ingest
    DELETE /documents/{document_id}        → 删除 Document（幂等，204；Phase 2.11 Step 2）

依赖注入（通过 DI 工厂，不直接实例化任何 Repository / Service）：
    - Depends(get_current_user)                 → User（Bearer token，Phase 3.4 Step 4）
    - Depends(get_document_repository)          → DocumentRepository (Protocol)
    - Depends(get_document_ingest_service)      → DocumentIngestService
    - Depends(get_document_upload_service)      → DocumentUploadService
    - Depends(get_document_delete_service)      → DocumentDeleteService（Phase 2.11 Step 2）
    - Depends(get_user_service)                 → UserService（解密 API Key，Phase 3.4 Step 4）

职责边界：
    - 接收 Pydantic Schema + 校验；
    - 解析当前用户身份（Bearer token）；Document 归属 = current_user.id
      （Phase 3.4 Step 4：user_id 不再由客户端传入）；
    - 解密当前用户的百炼 API Key，注入 upload / ingest 链路（Embedding 用用户自己的 Key）；
    - 调用 DocumentRepository.create_document() 创建记录；
    - 调用 DocumentIngestService.ingest_document() 执行生命周期 ingest
      （内部依次 get → DELETING gate → PROCESSING → IngestService.ingest_page
       → SUCCESS/FAILED）；
    - 调用 DocumentDeleteService.delete_document() 执行幂等删除
      （内部依次 Milvus chunks → FileStorage 文件 → MySQL 行；不存在也返回 204）；
    - 构造 DocumentResponse / DocumentIngestResponse 返回；
    - 不接触 EmbeddingClient / MilvusRepository / pymilvus / openai / SQLAlchemy Session。

异常处理：
    - DocumentNotFoundError  / DocumentOperationError  由 main.py 全局 handler 转
      HTTPException（404 / 503）；
    - MilvusRepositoryError / EmbeddingClientError     由既有全局 handler 转 503 / 502；
    - 本 Router 不 try/except 吞异常。

与现有 POST /ingest/page 的关系：
    /ingest/page 是底层 chunk-level 直接入库 API，保留且不修改；
    本 Router 的 POST /documents/{id}/ingest 是 Document 生命周期 API，
    通过 DocumentIngestService 复用 IngestService，形成：
        /documents/{id}/ingest → DocumentIngestService → IngestService → Milvus
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile, status

from ...core.di import (
    get_document_delete_service,
    get_document_ingest_service,
    get_document_repository,
    get_document_upload_service,
    get_user_service,
)
from ...models.document_api_schema import (
    DocumentCreateRequest,
    DocumentIngestRequest,
    DocumentIngestResponse,
    DocumentResponse,
    DocumentUploadResponse,
)
from ...models.user import User
from ...repositories.mysql import DocumentRepository
from ...services.document_delete import DocumentDeleteService
from ...services.document_ingest import DocumentIngestService
from ...services.document_upload import DocumentUploadService
from ...services.user_service import UserService
from ..deps import get_current_user

# 创建 Router（prefix + tags 与既有 ingest/rag Router 风格一致）
router: APIRouter = APIRouter(
    prefix="/documents",
    tags=["documents"],
)


@router.post(
    "",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建 Document",
    description=(
        "创建一条 Document 元数据记录（status=PENDING, chunk_count=0）；"
        "Document.id 与 Milvus page_id 为 1:1 映射（方案 A）。"
    ),
)
def create_document(
    request: DocumentCreateRequest,
    current_user: User = Depends(get_current_user),
    document_repository: DocumentRepository = Depends(get_document_repository),
) -> DocumentResponse:
    """
    POST /documents

    业务流程：
        1) current_user 由 Bearer token 解析（无效 → 401）；
        2) document_repository.create_document(filename, file_path,
           user_id=current_user.id) → 插入 documents 行，status 默认 PENDING、
           chunk_count 默认 0（ORM default + DB server_default 双层保证）；
        3) 构造 DocumentResponse 返回。

    Args:
        request: DocumentCreateRequest（filename / file_path 必填；
            user_id 已从 Schema 移除，归属 = current_user.id）。
        current_user: 当前登录用户（Bearer token → User；Phase 3.4 Step 4）。
        document_repository: 通过 DI 注入的 DocumentRepository（Protocol）实例。

    Returns:
        DocumentResponse: 新建 Document 的 id / filename / file_path / status / chunk_count。

    Raises:
        AuthenticationError: token 缺失 / 无效（401）。
        DocumentOperationError: 数据库执行失败（由全局 handler 转 503）。
    """
    document = document_repository.create_document(
        filename=request.filename,
        file_path=request.file_path,
        user_id=current_user.id,
    )
    return DocumentResponse(
        id=document.id,
        filename=document.filename,
        file_path=document.file_path,
        status=document.status,
        chunk_count=document.chunk_count,
    )


@router.post(
    "/{document_id}/ingest",
    response_model=DocumentIngestResponse,
    status_code=status.HTTP_200_OK,
    summary="Document 生命周期 ingest",
    description=(
        "将已切分的 chunk 文本列表入库 Milvus，并驱动 Document 生命周期："
        "get_document → PROCESSING → IngestService.ingest_page(page_id=document_id) "
        "→ SUCCESS(chunk_count=len(chunks)) / FAILED。"
    ),
)
async def ingest_document(
    document_id: int,
    request: DocumentIngestRequest,
    current_user: User = Depends(get_current_user),
    document_ingest_service: DocumentIngestService = Depends(
        get_document_ingest_service
    ),
    document_repository: DocumentRepository = Depends(get_document_repository),
    user_service: UserService = Depends(get_user_service),
) -> DocumentIngestResponse:
    """
    POST /documents/{document_id}/ingest

    业务流程：
        1) current_user 由 Bearer token 解析（无效 → 401）；
        2) document_ingest_service.ingest_document(document_id, chunks,
           user_id=current_user.id, api_key=decrypt_api_key(current_user))
           执行 Document 生命周期（见 services/document_ingest.py docstring；
           Phase 3.4 Step 4：Embedding 使用用户自己的百炼 Key，ingest 目标
           Document 必须是当前用户所有——跨用户 → DocumentNotFoundError）；
        3) 成功后重新读取 Document 终态（SUCCESS + chunk_count=len(chunks)），
           构造 DocumentIngestResponse 返回。

    Args:
        document_id: 路径参数，Document 主键 ID（= Milvus page_id，方案 A）。
        request: DocumentIngestRequest（chunks 非空列表）。
        current_user: 当前登录用户（Bearer token → User；Phase 3.4 Step 4）。
        document_ingest_service: 通过 DI 注入的 DocumentIngestService 实例。
        document_repository: 通过 DI 注入的 DocumentRepository（终态读取）。
        user_service: 通过 DI 注入的 UserService（解密当前用户的 API Key）。

    Returns:
        DocumentIngestResponse: document_id / status / chunk_count。

    Raises:
        AuthenticationError: token 缺失 / 无效（401）。
        SecurityDecryptionError: API Key 解密失败（500）。
        DocumentNotFoundError: document_id 不存在 / 跨用户（由全局 handler 转 404）。
        DocumentOperationError: 数据库执行失败（由全局 handler 转 503）。
        MilvusRepositoryError / EmbeddingClientError: Milvus / 百炼侧失败
            （由既有全局 handler 转 503 / 502）。
    """
    await document_ingest_service.ingest_document(
        document_id=document_id,
        chunks=request.chunks,
        user_id=current_user.id,
        api_key=user_service.decrypt_api_key(current_user),
    )
    # 读取终态构造响应（ingest_document 返回 None；服务侧禁止修改）
    # Phase 3.4 Step D：get_document(document_id, user_id) 二次 ownership 校验
    document = document_repository.get_document(document_id, current_user.id)
    return DocumentIngestResponse(
        document_id=document.id,
        status=document.status,
        chunk_count=document.chunk_count,
    )


@router.post(
    "/upload",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="上传并入库文档",
    description=(
        "multipart 上传文件，串起完整链路：输入校验 → LocalFileStorage.save() "
        "→ create_document(PENDING) → Parser.parse() → Chunker.split() "
        "→ DocumentIngestService.ingest_document() → SUCCESS。"
        "支持 .txt / .md / .markdown；文件超限 / 空文件 / 不支持扩展名分别返回 413 / 400 / 415。"
    ),
)
async def upload_document(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    upload_service: DocumentUploadService = Depends(get_document_upload_service),
    user_service: UserService = Depends(get_user_service),
) -> DocumentUploadResponse:
    """
    POST /documents/upload

    业务流程：
        1) current_user 由 Bearer token 解析（无效 → 401）；
        2) 读取 multipart 文件完整字节（content）与元信息（filename / content_type）；
        3) upload_service.upload(filename, content, user_id=current_user.id,
           mime_type, api_key=decrypt_api_key(current_user)) 执行完整上传链路
           （见 services/document_upload.py docstring；Phase 3.4 Step 4：
           Document 归属当前用户，Embedding 使用用户自己的百炼 Key）；
        4) 成功后构造 DocumentUploadResponse 返回（201）。

    Args:
        file: multipart 文件字段（UploadFile；filename 可为空串，由 Service 拒绝）。
        current_user: 当前登录用户（Bearer token → User；Phase 3.4 Step 4）。
        upload_service: 通过 DI 注入的 DocumentUploadService 实例。
        user_service: 通过 DI 注入的 UserService（解密当前用户的 API Key）。

    Returns:
        DocumentUploadResponse: id / filename / file_size / mime_type / status /
        chunk_count / error_message（成功时 status=SUCCESS, error_message=None）。

    Raises:
        AuthenticationError: token 缺失 / 无效（401）。
        SecurityDecryptionError: API Key 解密失败（500）。
        DocumentUploadError 族: 输入校验失败（400 / 413 / 415，全局 handler 转换）。
        DocumentParserError / DocumentChunkingError / DocumentRepositoryError /
        MilvusRepositoryError / EmbeddingClientError: 链路失败（500 / 503 / 502）。
    """
    content = await file.read()
    document = await upload_service.upload(
        filename=file.filename or "",
        content=content,
        user_id=current_user.id,
        mime_type=file.content_type,
        api_key=user_service.decrypt_api_key(current_user),
    )
    return DocumentUploadResponse(
        id=document.id,
        filename=document.filename,
        file_size=document.file_size,
        mime_type=document.mime_type,
        status=document.status,
        chunk_count=document.chunk_count,
        error_message=document.error_message,
    )


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除 Document",
    description=(
        "删除文档（幂等）：Milvus chunks → FileStorage 物理文件 → MySQL document 行；"
        "document 不存在也返回 204（目标态「文档不存在」已达成）。"
        "删除过程中 Document.status 置 DELETING，禁止 ingest/retry 并发写入；"
        "MySQL document 行删除为最终提交点。"
    ),
)
async def delete_document(
    document_id: int,
    current_user: User = Depends(get_current_user),
    document_delete_service: DocumentDeleteService = Depends(
        get_document_delete_service
    ),
):
    """
    DELETE /documents/{document_id}

    业务流程：
        1) current_user 由 Bearer token 解析（无效 → 401）；
        2) document_delete_service.delete_document(document_id,
           user_id=current_user.id) 执行幂等删除（见 services/document_delete.py
           docstring；Phase 3.4 Step C/D：ownership 约束——用户 A 删除用户 B 的
           文档按「不存在」处理，不泄露归属）：
           get → PROCESSING gate → DELETING → Milvus chunks → FileStorage 文件
           → MySQL document 行；document 不存在直接视为完成；
        3) 成功 / 幂等成功均返回 204 No Content（无响应 body）。

    Args:
        document_id: 路径参数，Document 主键 ID（= Milvus page_id，方案 A）。
        current_user: 当前登录用户（Bearer token → User；Phase 3.4 Step 4）。
        document_delete_service: 通过 DI 注入的 DocumentDeleteService 实例。

    Returns:
        None（FastAPI 返回 204 No Content，无 body）。

    Raises:
        AuthenticationError: token 缺失 / 无效（401）。
        DocumentNotFoundError: 已由 Service 幂等吸收，不会到达本层。
        DocumentOperationError: PROCESSING 状态下拒绝删除 / MySQL 失败（503）。
        MilvusRepositoryError / DocumentStorageError: Milvus / 文件删除失败
            （503 / 5xx，由既有全局 handler 转换）。
    """
    await document_delete_service.delete_document(
        document_id,
        current_user.id,
    )
