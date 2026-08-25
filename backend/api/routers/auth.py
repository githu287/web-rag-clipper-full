"""
认证 Router（Phase 3.4 Step 4 新增：用户身份接入 / API Key 注入）。

端点：
    POST /auth/register : 用百炼 API Key 注册，签发首个 Bearer token。
    POST /auth/login    : 已注册用户登录，轮换 token（旧 token 立即失效）。
    PUT  /auth/api-key  : 已登录用户更换自己的 API Key（token 不变）。

安全要点：
    - 明文 API Key 仅存在于请求体内存，不写日志 / 不落库明文
      （落库的是 SHA-256 hash + AES-256-GCM 密文 + 独立 nonce）；
    - 错误消息不含任何 Key / token 片段；
    - 业务 API（clips / documents / rag）一律走 Bearer token
      （见 backend/api/deps.py get_current_user）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from ...core.di import get_user_service
from ...models.api_schema import (
    ApiKeyAuthRequest,
    ApiKeyUpdateRequest,
    ApiKeyUpdateResponse,
    AuthResponse,
)
from ...models.user import User
from ...services.user_service import UserService
from ..deps import get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=AuthResponse,
    summary="用百炼 API Key 注册并签发 token",
)
def register(
    body: ApiKeyAuthRequest,
    user_service: UserService = Depends(get_user_service),
) -> AuthResponse:
    """
    以「用户自己的百炼 API Key」注册新用户。

    - 新 Key：创建用户并签发首个 Bearer token；
    - 已注册 Key：409 Conflict（请改走 /auth/login）。

    Response：
        user_id + token（token 明文仅本次返回一次；DB 只存 SHA-256 hash）。
    """
    user, token = user_service.register(body.api_key)
    return AuthResponse(user_id=user.id, token=token)


@router.post(
    "/login",
    response_model=AuthResponse,
    summary="API Key 登录（每次登录轮换 token）",
)
def login(
    body: ApiKeyAuthRequest,
    user_service: UserService = Depends(get_user_service),
) -> AuthResponse:
    """
    已注册用户登录：校验 API Key 并签发新 token（旧 token 立即失效）。

    - 未注册 Key：401（请先 /auth/register）；
    - 用户被禁用（DISABLED）：401。

    Response：
        user_id + token（每次登录轮换，客户端用新 token 替换本地存储）。
    """
    user, token = user_service.login(body.api_key)
    return AuthResponse(user_id=user.id, token=token)


@router.put(
    "/api-key",
    response_model=ApiKeyUpdateResponse,
    summary="更换自己的 API Key（需 Bearer token）",
)
def update_api_key(
    body: ApiKeyUpdateRequest,
    current_user: User = Depends(get_current_user),
    user_service: UserService = Depends(get_user_service),
) -> ApiKeyUpdateResponse:
    """
    已登录用户更换自己的百炼 API Key（需 Bearer token）。

    更换后：
        - 新 Key 立即生效（后续 embedding / LLM 调用自动使用）；
        - token / user_id / 知识库归属均不变（客户端继续用原 token）。

    Response：
        user_id（客户端无需处理，仅确认生效）。
    """
    user = user_service.update_api_key(current_user.id, body.api_key)
    return ApiKeyUpdateResponse(user_id=user.id)
