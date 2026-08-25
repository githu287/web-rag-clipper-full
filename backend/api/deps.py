"""
FastAPI 依赖（Phase 3.4 Step 4 新增：当前用户身份解析）。

get_current_user：
    从 `Authorization: Bearer <token>` 解析 token → SHA-256 → users.token_hash
    精确查询 → 返回 User ORM；任何一步失败 → AuthenticationError(401)。

设计要点：
    1) 只负责「请求者是谁」：API Key 解密由 Router 显式调用
       UserService.decrypt_api_key(user)，不在本依赖内隐式执行
       （依赖保持最小职责，解密属于业务链路而非认证）。
    2) 错误消息不含任何 token 片段（明文 / hash 一律不进日志）。
    3) UserRepository 查询失败（UserOperationError）不在这里捕获，
       由 main.py 全局 handler 映射 503。

范围边界：
    - 不检查 IP / User-Agent / 限流（未来可扩展为独立中间件）；
    - 不轮换 token（token 轮换只发生在 /auth/login）。
"""

from __future__ import annotations

from fastapi import Depends, Header

from ..core.di import get_user_repository
from ..core.exceptions import AuthenticationError
from ..core.security import hash_token
from ..models.user import User, UserStatus
from ..repositories.mysql.user_protocol import UserRepository

_BEARER_PREFIX: str = "Bearer "


def get_current_user(
    authorization: str | None = Header(default=None),
    user_repository: UserRepository = Depends(get_user_repository),
) -> User:
    """
    当前用户依赖：`Authorization: Bearer <token>` → User ORM。

    Args:
        authorization: 请求头 Authorization（默认 None = 未提供）。
        user_repository: UserRepository（Protocol），DI 注入。

    Returns:
        当前登录用户 ORM 对象（status = ACTIVE）。

    Raises:
        AuthenticationError(401)：
            - 缺少 Authorization 头 / 非 "Bearer <token>" 格式 / token 为空；
            - token 对应 token_hash 在 users 表查不到（无效 / 已过期 / 已轮换）；
            - 用户 status != ACTIVE（DISABLED）。
    """
    if not authorization or not authorization.startswith(_BEARER_PREFIX):
        raise AuthenticationError(
            "missing or invalid Authorization header (Bearer token required)"
        )

    token = authorization[len(_BEARER_PREFIX):].strip()
    if not token:
        raise AuthenticationError("empty bearer token")

    user = user_repository.get_user_by_token_hash(hash_token(token))
    if user is None:
        raise AuthenticationError("invalid or expired token")
    if user.status != UserStatus.ACTIVE:
        raise AuthenticationError("user is disabled")

    return user
