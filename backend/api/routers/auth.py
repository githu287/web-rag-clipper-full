"""
认证 Router（Phase 3.4 Step 4；F-REV3 身份重构：username + password 认证）。

端点：
    POST /auth/register : 注册（仅 username + password），签发首个 Bearer token。
    POST /auth/login    : 登录（仅 username + password），轮换 token（旧 token 失效）。
    POST /auth/logout   : 登出（Bearer token），清除会话（旧 token 立即 401）。

API Key 配置端点已迁移至 users Router（F-REV3 决策：API Key 不参与身份）：
    PUT    /users/me/api-key : 配置 / 更换自己的百炼 API Key（Bearer）。
    DELETE /users/me/api-key : 清除自己的百炼 API Key（Bearer）。
    旧 PUT /auth/api-key 已删除，不保留两套 API。

安全要点：
    - 注册 / 登录完全脱离第三方服务（不调百炼、不校验 / 不加密 API Key）；
    - 明文 password 仅存在于请求体内存，不写日志 / 不落库明文
      （落库的是 Argon2id password_hash）；
    - token 明文仅返回一次，DB 只存 SHA-256 hash；
    - 错误消息不含任何 password / token / API Key 片段。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from ...core.di import get_user_service
from ...models.api_schema import (
    AuthResponse,
    LoginRequest,
    RegisterRequest,
)
from ...models.user import User
from ...services.user_service import UserService
from ..deps import get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=AuthResponse,
    summary="注册（仅 username + password）并签发 token",
)
def register(
    body: RegisterRequest,
    user_service: UserService = Depends(get_user_service),
) -> AuthResponse:
    """
    以 username + password 注册新用户（不需要 API Key）。

    - username 已存在：409 Conflict（请改走 /auth/login）；
    - 密码强度不足（< 8 / > 128 字符等）：422。

    Response：
        user_id + token（token 明文仅本次返回一次；DB 只存 SHA-256 hash）。
    """
    user, token = user_service.register(body.username, body.password)
    return AuthResponse(user_id=user.id, token=token)


@router.post(
    "/login",
    response_model=AuthResponse,
    summary="登录（仅 username + password，每次登录轮换 token）",
)
def login(
    body: LoginRequest,
    user_service: UserService = Depends(get_user_service),
) -> AuthResponse:
    """
    以 username + password 登录并签发新 token（旧 token 立即失效）。

    - username 不存在与密码错误统一 401（防用户枚举）；
    - 用户被禁用（DISABLED）：403。

    Response：
        user_id + token（每次登录轮换，客户端用新 token 替换本地存储）。
    """
    user, token = user_service.login(body.username, body.password)
    return AuthResponse(user_id=user.id, token=token)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="登出（需 Bearer token；立即失效当前会话）",
)
def logout(
    current_user: User = Depends(get_current_user),
    user_service: UserService = Depends(get_user_service),
) -> Response:
    """
    已登录用户登出：清除 users.token_hash（旧 token 立即 401）。

    要求：
        Authorization: Bearer <token>

    不创建 refresh token、不引入 Redis。
    登出后需重新 /auth/login 才能获得新 token。
    """
    user_service.logout(current_user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
