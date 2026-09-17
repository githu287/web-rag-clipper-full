"""
用户信息 / 模型配置 Router（Phase 3.4 Step 4；F-REV3 身份重构）。

端点：
    GET    /users/me           : 当前用户信息（Bearer token）。
    PUT    /users/me/api-key   : 配置 / 更换自己的百炼 API Key（Bearer）。
    DELETE /users/me/api-key   : 清除自己的百炼 API Key（Bearer）。

设计要点：
    - 所有端点必须 Bearer token（get_current_user），身份唯一来源
      token → token_hash → users.id；前端不传 user_id。
    - API Key 仅通过 users.id 关联：配置 / 清除都不改变
      user_id / username / password_hash / token_hash / documents 归属。
    - GET /users/me 安全红线：不返回 api_key / ciphertext / nonce /
      password_hash / token_hash / APP_MASTER_KEY。
    - 本 Router 不直接操作 Repository / SQL（全部经 UserService）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from ...core.di import get_user_service
from ...models.api_schema import (
    ApiKeyUpdateRequest,
    ApiKeyUpdateResponse,
    UserMeResponse,
)
from ...models.user import User
from ...services.user_service import UserService
from ..deps import get_current_user

router = APIRouter(prefix="/users", tags=["users"])


@router.get(
    "/me",
    response_model=UserMeResponse,
    summary="当前用户信息（需 Bearer token）",
)
def get_me(
    current_user: User = Depends(get_current_user),
    user_service: UserService = Depends(get_user_service),
) -> UserMeResponse:
    """
    返回当前登录用户信息（重新查询保证一致性）。

    Response：
        user_id + username + api_key_configured + created_at。
    """
    user = user_service.get_current_user(current_user.id)
    return UserMeResponse(
        user_id=user.id,
        username=user.username,
        api_key_configured=(
            user.api_key_ciphertext is not None
            and user.api_key_nonce is not None
        ),
        created_at=user.created_at,
    )


@router.put(
    "/me/api-key",
    response_model=ApiKeyUpdateResponse,
    summary="配置 / 更换自己的百炼 API Key（需 Bearer token）",
)
def update_api_key(
    body: ApiKeyUpdateRequest,
    current_user: User = Depends(get_current_user),
    user_service: UserService = Depends(get_user_service),
) -> ApiKeyUpdateResponse:
    """
    已登录用户配置 / 更换自己的百炼 API Key。

    - 格式（sk- 前缀）由请求体 Pydantic 校验（422）；
    - 有效性由 UserService 用「用户提交的 Key」调百炼最小 embedding 验证
      （失败 400）；
    - 更换后：user_id / username / password_hash / token_hash /
      documents 归属均不变（客户端继续用原 token）。

    Response：
        user_id（客户端无需处理，仅确认生效）。
    """
    user = user_service.update_api_key(current_user.id, body.api_key)
    return ApiKeyUpdateResponse(user_id=user.id)


@router.delete(
    "/me/api-key",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="清除自己的百炼 API Key（需 Bearer token）",
)
def remove_api_key(
    current_user: User = Depends(get_current_user),
    user_service: UserService = Depends(get_user_service),
) -> Response:
    """
    已登录用户清除自己的百炼 API Key（ciphertext / nonce → NULL）。

    不删除用户 / documents / token / password / username；
    清除后账号仍可正常登录，仅百炼调用需重新配置 Key。
    """
    user_service.remove_api_key(current_user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
