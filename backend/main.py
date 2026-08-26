"""
FastAPI 应用入口（Phase 2.7 Step 2）。

职责：
    1) create_app() 工厂函数：创建 FastAPI 实例 + 注册 Router + 注册全局异常处理器；
    2) lifespan 异步上下文管理器：启动期调用 MilvusInitializer.initialize() 建 Collection + Index + Load。

依赖注入：
    - Router 通过 Depends(get_ingest_service / get_rag_service) 注入 Service；
    - lifespan 通过 core.di.get_milvus_initializer() 获取 Initializer（不走 Depends，因为
      lifespan 不在请求上下文内）。

生命周期：
    - import 阶段：仅定义 create_app，不创建 FastAPI 实例，不连接外部服务；
    - create_app() 阶段：创建 FastAPI + include_router + 注册 handler（不连接）；
    - lifespan startup 阶段：调用 MilvusInitializer.initialize()（首次连接 Milvus 建 Collection）；
    - 请求阶段：Depends 触发 Service 注入（不连接；Service 方法调用时才惰性连接）。

异常处理：
    - MilvusRepositoryError → 503 Service Unavailable（Milvus 侧问题，可重试）；
    - EmbeddingClientError → 502 Bad Gateway（百炼侧问题，可重试）；
    - 未知异常不吞，继续向上抛出（FastAPI 默认转 500）。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .api.routers import auth as auth_router_module
from .api.routers import clips as clips_router_module
from .api.routers import documents as documents_router_module
from .api.routers import ingest as ingest_router_module
from .api.routers import rag as rag_router_module
from .api.routers import users as users_router_module
from .clients.embedding import EmbeddingClientError
from .clients.llm import LLMClientError
from .core.di import get_milvus_initializer
from .core.exceptions import (
    ApiKeyAlreadyRegisteredError,
    ApiKeyInvalidError,
    ApiKeyNotConfiguredError,
    ApiKeyValidationError,
    AuthenticationError,
    AuthOperationError,
    DisabledUserError,
    InvalidCredentialsError,
    PasswordPolicyError,
    UsernameAlreadyExistsError,
    DocumentChunkingError,
    DocumentFileEmptyError,
    DocumentFileTooLargeError,
    DocumentNotFoundError,
    DocumentNotSuccessError,
    DocumentOperationError,
    DocumentParserError,
    DocumentParserUnsupportedExtensionError,
    DocumentStorageError,
    DocumentStoragePathTraversalError,
    DocumentUnsupportedExtensionError,
    DocumentUploadError,
    MilvusRepositoryError,
    SecurityConfigurationError,
    SecurityDecryptionError,
    UserNotFoundError,
    UserOperationError,
)

logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan：启动期初始化 Milvus Collection
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    FastAPI lifespan 异步上下文管理器。

    启动阶段（yield 之前）：
        调用 MilvusInitializer.initialize() 确保 page_chunks Collection + Index 就位 + Load 完成。
        - 通过 core.di.get_milvus_initializer() 获取 Initializer（不走 Depends，lifespan 无请求上下文）；
        - initialize() 内部幂等：Collection 已存在则跳过，不删重建；
        - 失败则记录日志并向上抛出（应用启动失败，由 uvicorn / 容器编排决定重试策略）。

    关闭阶段（yield 之后）：
        当前无需清理资源（MilvusClient 由 Repository Impl 每次方法调用时 with 上下文自动释放；
        OpenAI Client 由 EmbeddingClient 持有，进程退出时由 OS 回收）。

    Args:
        app: FastAPI 应用实例（lifespan 协议要求；本实现未直接使用）。
    """
    logger.info("FastAPI lifespan startup: 开始初始化 Milvus Collection...")
    initializer = get_milvus_initializer()
    initializer.initialize()  # 幂等：Collection 已存在则跳过
    logger.info("FastAPI lifespan startup: Milvus Collection 初始化完成")
    yield
    logger.info("FastAPI lifespan shutdown: 无需清理资源")


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """
    创建并返回 FastAPI 应用实例。

    装配步骤：
        1) 创建 FastAPI 实例（绑定 lifespan）；
        2) 注册全局异常处理器（MilvusRepositoryError / EmbeddingClientError → HTTPException；
           Phase 3.4 Step 4 新增 Auth / User / Security 异常 handler）；
        3) include_router 注册 ingest + rag + documents + clips + auth + users
           （Phase 3.4 Step 4 认证 / 用户 / API Key 配置）路由器。

    严格不执行：
        - 不在 import 阶段创建 FastAPI 实例（由调用方 / uvicorn 显式调用 create_app()）；
        - 不在 create_app() 阶段连接外部服务（Milvus 连接由 lifespan startup 触发）；
        - 不直接实例化 Service / EmbeddingClient / PyMilvusRepositoryImpl（由 DI 工厂装配）。

    Returns:
        FastAPI: 已装配 Router + 异常处理器 + lifespan 的应用实例。
    """
    app = FastAPI(
        title="web-rag-clipper-full",
        description="RAG 系统 API（Phase 2.7 Step 2）",
        version="0.1.0",
        lifespan=lifespan,
    )

    # -------------------------------------------------- 注册全局异常处理器
    _register_exception_handlers(app)

    # -------------------------------------------------- 注册路由器
    app.include_router(ingest_router_module.router)
    app.include_router(rag_router_module.router)
    app.include_router(documents_router_module.router)
    app.include_router(clips_router_module.router)
    app.include_router(auth_router_module.router)  # Phase 3.4 Step 4：认证体系
    app.include_router(users_router_module.router)  # Phase 3.4 Step 4：用户信息 / API Key 配置

    return app


# ---------------------------------------------------------------------------
# 异常处理器注册
# ---------------------------------------------------------------------------


def _register_exception_handlers(app: FastAPI) -> None:
    """
    注册全局异常处理器（私有辅助函数）。

    映射规则：
        - MilvusRepositoryError → 503 Service Unavailable
          （Milvus 侧问题：连接失败 / 操作失败 / Schema 不匹配；可重试）
        - EmbeddingClientError → 502 Bad Gateway
          （百炼侧问题：API Key 错误 / 限流 / 超时 / 响应异常；可重试）
        - DocumentUploadError 族 → 400 / 413 / 415（客户端输入校验失败，Phase 2.10 Step 3）
        - DocumentStorageError 族 → 400（路径穿越）/ 500（存储 IO 失败）
        - DocumentParserError 族 → 400（不支持扩展名，防御）/ 500（读取失败）
        - DocumentChunkingError 族 → 500（切分内部错误）
        - AuthenticationError → 401（token 无效 / 缺失，Phase 3.4 Step 4）
        - ApiKeyInvalidError → 401 / ApiKeyAlreadyRegisteredError → 409（旧 API Key
          身份模型；F-REV3 后不再被新代码抛出，F5 清理阶段统一移除）
        - InvalidCredentialsError → 401（username 不存在 / 密码错误，统一语义，F-REV3）
        - DisabledUserError → 403（账号被禁用，F-REV3）
        - UsernameAlreadyExistsError → 409（注册重名，F-REV3）
        - PasswordPolicyError → 422（密码强度不满足，F-REV3）
        - ApiKeyValidationError → 400（API Key 验证失败，F-REV3）
        - ApiKeyNotConfiguredError → 409（未配置 API Key，F-REV3）
        - AuthOperationError / UserOperationError → 503（用户数据服务问题，Phase 3.4 Step 4）
        - UserNotFoundError → 404（用户不存在，Phase 3.4 Step 4）
        - SecurityConfigurationError / SecurityDecryptionError → 500（安全配置 / 解密失败）

    严格不吞未知异常：
        - 其他 Exception 不在本处理器范围内，由 FastAPI 默认机制转 500 Internal Server Error；
        - 不使用 `except Exception: pass` 吞异常。
    """

    @app.exception_handler(MilvusRepositoryError)
    async def handle_milvus_error(
        request: Request,
        exc: MilvusRepositoryError,
    ) -> JSONResponse:
        """
        MilvusRepositoryError → 503 Service Unavailable。

        包含子类：
            - MilvusConnectionError（连接失败，可重试）
            - MilvusOperationError（操作失败，按场景重试）
            - MilvusSchemaMismatchError（契约错，不可重试；但仍返回 503 让上层决定）
        """
        logger.exception("MilvusRepositoryError: %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "detail": f"Milvus 服务异常：{exc}",
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(EmbeddingClientError)
    async def handle_embedding_error(
        request: Request,
        exc: EmbeddingClientError,
    ) -> JSONResponse:
        """
        EmbeddingClientError → 502 Bad Gateway。

        包含子类：
            - EmbeddingConfigError（配置错，不可重试；API Key 缺失等）
            - EmbeddingAPIError（百炼 API 错，可重试）
            - EmbeddingResponseError（响应错，不可重试；维度不匹配等）
        """
        logger.exception("EmbeddingClientError: %s", exc)
        return JSONResponse(
            status_code=502,
            content={
                "detail": f"Embedding 服务异常：{exc}",
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(LLMClientError)
    async def handle_llm_error(
        request: Request,
        exc: LLMClientError,
    ) -> JSONResponse:
        """
        LLMClientError → 502 Bad Gateway（Phase 3.3 Step 3 新增）。

        包含子类：
            - LLMClientConfigError（配置错，不可重试；API Key / model 缺失等）
            - LLMClientRequestError（百炼 Chat API 错，可重试；网络 / 超时 / 限流 / 5xx）
            - LLMClientResponseError（响应契约错，不可重试；choices / content 缺失）
            - LLMClientEmptyResponseError（空响应，不可重试；content None / 空字符串）
        """
        logger.exception("LLMClientError: %s", exc)
        return JSONResponse(
            status_code=502,
            content={
                "detail": f"LLM 服务异常：{exc}",
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(DocumentNotSuccessError)
    async def handle_document_not_success_error(
        request: Request,
        exc: DocumentNotSuccessError,
    ) -> JSONResponse:
        """
        DocumentNotSuccessError → 409 Conflict（Phase 3.3 Step 3 新增）。

        触发场景：
            RagAnswerService.ask 指定 document_id 且该 Document 状态不是 SUCCESS
            （PENDING / PROCESSING / FAILED / DELETING）。

        处理策略：不可重试（重试仍不是 SUCCESS）；业务冲突语义，返回 409。
        """
        logger.warning("DocumentNotSuccessError -> 409: %s", exc)
        return JSONResponse(
            status_code=409,
            content={
                "detail": f"Document 状态不允许回答：{exc}",
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(DocumentNotFoundError)
    async def handle_document_not_found(
        request: Request,
        exc: DocumentNotFoundError,
    ) -> JSONResponse:
        """
        DocumentNotFoundError → 404 Not Found。

        触发场景：
            - get_document(doc_id) 查询返回 None；
            - update_status / update_ingest_result / delete_document 操作 0 行。

        处理策略：不可重试（重试仍是不存在），直接返回 404 由客户端处理。
        """
        logger.exception("DocumentNotFoundError: %s", exc)
        return JSONResponse(
            status_code=404,
            content={
                "detail": f"Document 不存在：{exc}",
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(DocumentOperationError)
    async def handle_document_operation_error(
        request: Request,
        exc: DocumentOperationError,
    ) -> JSONResponse:
        """
        DocumentOperationError → 503 Service Unavailable。

        与 MilvusRepositoryError→503 风格对齐（外部数据服务问题）：
            - sqlalchemy.exc.OperationalError（连接/超时/死锁等，可重试）
            - sqlalchemy.exc.IntegrityError（约束冲突，不可重试）
            - sqlalchemy.exc.DBAPIError（其他底层 DBAPI 错误）

        统一 503 让上层客户端按服务暂不可用处理并决定重试策略。
        """
        logger.exception("DocumentOperationError: %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "detail": f"Document 数据服务异常：{exc}",
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(DocumentUploadError)
    async def handle_document_upload_error(
        request: Request,
        exc: DocumentUploadError,
    ) -> JSONResponse:
        """
        DocumentUploadError 族 → 4xx（Phase 2.10 Step 3 上传入口业务校验失败）。

        子类映射（客户端输入问题，不可重试）：
            - DocumentFileTooLargeError          → 413 Payload Too Large
              （len(content) > max_page_content_bytes，超限不落盘不建 Document）
            - DocumentFileEmptyError             → 400 Bad Request
              （0 字节文件，禁止生成空 SUCCESS Document）
            - DocumentUnsupportedExtensionError  → 415 Unsupported Media Type
              （本阶段仅 .txt/.md/.markdown；.pdf/.docx 明确拒绝）
            - 其他 DocumentUploadError           → 400 Bad Request
              （空文件名 / 含路径分隔符 / 切分结果为空等）

        4xx 属客户端错误，仅记录 warning（不产生堆栈噪音）。
        """
        if isinstance(exc, DocumentFileTooLargeError):
            status_code = 413
        elif isinstance(exc, DocumentUnsupportedExtensionError):
            status_code = 415
        else:
            status_code = 400
        logger.warning("DocumentUploadError -> %s: %s", status_code, exc)
        return JSONResponse(
            status_code=status_code,
            content={
                "detail": str(exc),
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(DocumentStorageError)
    async def handle_document_storage_error(
        request: Request,
        exc: DocumentStorageError,
    ) -> JSONResponse:
        """
        DocumentStorageError 族 → 400 / 500（文件落盘 / 删除失败）。

        子类映射：
            - DocumentStoragePathTraversalError → 400 Bad Request
              （客户端 filename 含 `..` / 路径分隔符等，UploadService 已提前拦截，
               此 handler 为深层防御）
            - 其他 DocumentStorageError          → 500 Internal Server Error
              （本地磁盘 IO 失败：权限 / 磁盘满 / 目录不可写；可重试）

        500 场景属服务端问题，记录 exception 完整堆栈。
        """
        if isinstance(exc, DocumentStoragePathTraversalError):
            logger.warning("DocumentStoragePathTraversalError -> 400: %s", exc)
            status_code = 400
        else:
            logger.exception("DocumentStorageError -> 500: %s", exc)
            status_code = 500
        return JSONResponse(
            status_code=status_code,
            content={
                "detail": str(exc),
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(DocumentParserError)
    async def handle_document_parser_error(
        request: Request,
        exc: DocumentParserError,
    ) -> JSONResponse:
        """
        DocumentParserError 族 → 400 / 500（文件解析失败）。

        子类映射：
            - DocumentParserUnsupportedExtensionError → 400 Bad Request
              （UploadService 已提前按扩展名拒绝，理论上不达 Parser；防御兜底）
            - DocumentParserReadError                  → 500 Internal Server Error
              （文件读取 / 解码失败：文件损坏、编码异常、IO 权限；可重试）

        Parser 读取失败属服务端 IO 问题，记录 exception 完整堆栈。
        """
        if isinstance(exc, DocumentParserUnsupportedExtensionError):
            logger.warning(
                "DocumentParserUnsupportedExtensionError -> 400: %s", exc
            )
            status_code = 400
        else:
            logger.exception("DocumentParserError -> 500: %s", exc)
            status_code = 500
        return JSONResponse(
            status_code=status_code,
            content={
                "detail": str(exc),
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(DocumentChunkingError)
    async def handle_document_chunking_error(
        request: Request,
        exc: DocumentChunkingError,
    ) -> JSONResponse:
        """
        DocumentChunkingError 族 → 500 Internal Server Error。

        触发场景：
            - DocumentChunkingConfigError：chunker 配置非法（chunk_size / overlap 约束
              被破坏，属部署配置缺陷，应修配置后重启）；
            - 切分执行期异常（不可预期的内部错误）。

        统一 500：配置 / 内部错误均属服务端问题，记录 exception 完整堆栈。
        """
        logger.exception("DocumentChunkingError: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "detail": f"Document 切分异常：{exc}",
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(AuthenticationError)
    async def handle_authentication_error(
        request: Request,
        exc: AuthenticationError,
    ) -> JSONResponse:
        """
        AuthenticationError → 401 Unauthorized（Phase 3.4 Step 4 新增）。

        触发场景（backend/api/deps.py get_current_user）：
            - Authorization 头缺失 / 非 "Bearer <token>" 格式 / token 为空；
            - token 无效 / 已过期 / 已轮换（users.token_hash 查不到）；
            - 用户被禁用（status != ACTIVE）。

        处理策略：不可重试（客户端需重新登录获取有效 token）。
        错误消息不含任何 token 片段。
        """
        logger.warning("AuthenticationError -> 401: %s", exc)
        return JSONResponse(
            status_code=401,
            content={
                "detail": f"认证失败：{exc}",
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(ApiKeyInvalidError)
    async def handle_api_key_invalid_error(
        request: Request,
        exc: ApiKeyInvalidError,
    ) -> JSONResponse:
        """
        ApiKeyInvalidError → 401 Unauthorized（Phase 3.4 Step 4 新增）。

        触发场景（UserService.login / register / update_api_key）：
            - api_key 为空 / 全空白；
            - login 时该 API Key 未注册；
            - 用户被禁用（DISABLED）。

        处理策略：不可重试（输入凭据本身无效），客户端应检查 API Key 后重试。
        不泄露「该 Key 是否存在」之外的信息。
        """
        logger.warning("ApiKeyInvalidError -> 401: %s", exc)
        return JSONResponse(
            status_code=401,
            content={
                "detail": f"API Key 无效：{exc}",
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(ApiKeyAlreadyRegisteredError)
    async def handle_api_key_already_registered_error(
        request: Request,
        exc: ApiKeyAlreadyRegisteredError,
    ) -> JSONResponse:
        """
        ApiKeyAlreadyRegisteredError → 409 Conflict（Phase 3.4 Step 4 新增）。

        触发场景（UserService.register）：
            - 该 API Key 已注册（客户端应改用 POST /auth/login）。

        处理策略：不可重试；409 语义明确引导客户端切换登录流程。
        """
        logger.warning("ApiKeyAlreadyRegisteredError -> 409: %s", exc)
        return JSONResponse(
            status_code=409,
            content={
                "detail": f"注册冲突：{exc}",
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(AuthOperationError)
    async def handle_auth_operation_error(
        request: Request,
        exc: AuthOperationError,
    ) -> JSONResponse:
        """
        AuthOperationError → 503 Service Unavailable（Phase 3.4 Step 4 新增）。

        触发场景（UserService 包装转发 UserOperationError）：
            - users 表 SQL 执行异常（连接 / 超时 / 唯一约束冲突）。

        与 DocumentOperationError → 503 风格对齐（外部数据服务问题）。
        """
        logger.exception("AuthOperationError: %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "detail": f"认证服务异常：{exc}",
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(UserNotFoundError)
    async def handle_user_not_found(
        request: Request,
        exc: UserNotFoundError,
    ) -> JSONResponse:
        """
        UserNotFoundError → 404 Not Found（Phase 3.4 Step 4 新增）。

        触发场景：
            - update_api_key / update_token 时 user_id 不存在。
              （get_current_user 已保证 user 存在，正常流程不可达；防御兜底）

        处理策略：不可重试（重试仍不存在）。
        """
        logger.exception("UserNotFoundError: %s", exc)
        return JSONResponse(
            status_code=404,
            content={
                "detail": f"用户不存在：{exc}",
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(UserOperationError)
    async def handle_user_operation_error(
        request: Request,
        exc: UserOperationError,
    ) -> JSONResponse:
        """
        UserOperationError → 503 Service Unavailable（Phase 3.4 Step 4 新增）。

        与 DocumentOperationError → 503 风格对齐（users 表外部数据服务问题）。
        """
        logger.exception("UserOperationError: %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "detail": f"用户数据服务异常：{exc}",
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(UsernameAlreadyExistsError)
    async def handle_username_already_exists_error(
        request: Request,
        exc: UsernameAlreadyExistsError,
    ) -> JSONResponse:
        """
        UsernameAlreadyExistsError → 409 Conflict（F-REV3 新增）。

        触发场景（UserService.register）：username 已被注册
        （客户端应改用 POST /auth/login）。
        处理策略：不可重试；错误消息只含 username（不含任何凭据）。
        """
        logger.warning("UsernameAlreadyExistsError -> 409: %s", exc)
        return JSONResponse(
            status_code=409,
            content={
                "detail": f"用户名已存在：{exc}",
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(InvalidCredentialsError)
    async def handle_invalid_credentials_error(
        request: Request,
        exc: InvalidCredentialsError,
    ) -> JSONResponse:
        """
        InvalidCredentialsError → 401 Unauthorized（F-REV3 新增）。

        触发场景（UserService.login）：
            - username 不存在；
            - password 错误。
        两者返回完全相同的语义（防用户枚举），错误消息统一
        "invalid username or password"，不含具体差异。
        """
        logger.warning("InvalidCredentialsError -> 401: %s", exc)
        return JSONResponse(
            status_code=401,
            content={
                "detail": f"登录失败：{exc}",
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(DisabledUserError)
    async def handle_disabled_user_error(
        request: Request,
        exc: DisabledUserError,
    ) -> JSONResponse:
        """
        DisabledUserError → 403 Forbidden（F-REV3 新增）。

        触发场景：
            - login 时用户 status != ACTIVE；
            - 已登录用户 token 对应账号被禁用。
        处理策略：不可重试；客户端应提示账号状态异常（联系管理员 / 重新注册）。
        """
        logger.warning("DisabledUserError -> 403: %s", exc)
        return JSONResponse(
            status_code=403,
            content={
                "detail": f"账号已被禁用：{exc}",
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(PasswordPolicyError)
    async def handle_password_policy_error(
        request: Request,
        exc: PasswordPolicyError,
    ) -> JSONResponse:
        """
        PasswordPolicyError → 422 Unprocessable Entity（F-REV3 新增）。

        触发场景（UserService.register）：
            - 密码长度 < 8 / > 128；
            - 未包含大写 / 小写 / 数字等强度要求。
        处理策略：不可重试；客户端按规则提示后重试。
        """
        logger.warning("PasswordPolicyError -> 422: %s", exc)
        return JSONResponse(
            status_code=422,
            content={
                "detail": f"密码不符合要求：{exc}",
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(ApiKeyValidationError)
    async def handle_api_key_validation_error(
        request: Request,
        exc: ApiKeyValidationError,
    ) -> JSONResponse:
        """
        ApiKeyValidationError → 400 Bad Request（F-REV3 新增）。

        触发场景（UserService.update_api_key）：
            - api_key 为空；
            - 用「用户提交的 Key」调百炼最小 embedding 验证失败。
        处理策略：不可重试；客户端应检查 Key 后重试。
        错误消息不含 API Key 明文 / 片段。
        """
        logger.warning("ApiKeyValidationError -> 400: %s", exc)
        return JSONResponse(
            status_code=400,
            content={
                "detail": f"API Key 校验失败：{exc}",
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(ApiKeyNotConfiguredError)
    async def handle_api_key_not_configured_error(
        request: Request,
        exc: ApiKeyNotConfiguredError,
    ) -> JSONResponse:
        """
        ApiKeyNotConfiguredError → 409 Conflict（F-REV3 新增）。

        触发场景（UserService.decrypt_api_key）：
            - 账号未配置百炼 API Key（ciphertext / nonce 为 NULL），
              业务链路（Embedding / LLM）需要用户 Key 时返回。
        处理策略：不可重试；客户端应引导用户前往 PUT /users/me/api-key 配置。
        消息固定为「当前账号尚未配置阿里云百炼 API Key，请前往设置配置。」
        """
        logger.warning("ApiKeyNotConfiguredError -> 409: %s", exc)
        return JSONResponse(
            status_code=409,
            content={
                "detail": f"API Key 未配置：{exc}",
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(SecurityConfigurationError)
    async def handle_security_configuration_error(
        request: Request,
        exc: SecurityConfigurationError,
    ) -> JSONResponse:
        """
        SecurityConfigurationError → 500 Internal Server Error（Phase 3.4 Step 4 新增）。

        触发场景：APP_MASTER_KEY 缺失 / 非 32 bytes（部署配置缺陷）。
        处理策略：服务端配置问题，需修 .env 后重启；记录 exception 完整堆栈。
        """
        logger.exception("SecurityConfigurationError: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "detail": f"安全配置异常：{exc}",
                "type": type(exc).__name__,
            },
        )

    @app.exception_handler(SecurityDecryptionError)
    async def handle_security_decryption_error(
        request: Request,
        exc: SecurityDecryptionError,
    ) -> JSONResponse:
        """
        SecurityDecryptionError → 500 Internal Server Error（Phase 3.4 Step 4 新增）。

        触发场景：AES 解密失败（密文 / nonce 损坏、主密钥被更换）。
        处理策略：服务端问题（数据损坏 / 配置变更），记录 exception 完整堆栈。
        """
        logger.exception("SecurityDecryptionError: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "detail": f"API Key 解密异常：{exc}",
                "type": type(exc).__name__,
            },
        )


# ---------------------------------------------------------------------------
# 模块级 app 实例
# ---------------------------------------------------------------------------

# 模块级 app 变量：支持两种 uvicorn 启动方式：
#   1) uvicorn backend.main:app              （直接引用模块级变量，最常用）
#   2) uvicorn backend.main:create_app --factory （工厂模式，便于测试时注入不同配置）
#
# 注意：app = create_app() 会在 import 阶段执行 create_app()，但不会触发 lifespan startup
# （lifespan 由 uvicorn 在事件循环启动后触发），因此 import 阶段不会连接 Milvus。
app: FastAPI = create_app()
