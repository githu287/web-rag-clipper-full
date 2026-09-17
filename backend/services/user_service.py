"""
UserService —— 用户认证业务编排（Phase 3.4 Step 4 用户身份接入）。

职责：
    在 core.security（纯安全计算）与 UserRepository（users 表持久化）之上，
    编排「API Key 即身份」的注册 / 登录 / 换 Key / 解密注入流程：
    - register(api_key)       ：新 API Key 注册并签发首个 Bearer token；
    - login(api_key)          ：已注册用户登录，轮换 token（旧 token 立即失效）；
    - update_api_key(user_id, api_key)：已登录用户更换自己的 API Key（token 不变）；
    - decrypt_api_key(user)   ：解密用户 API Key，供 Embedding / LLM 业务链路注入。

身份模型（与 core.security / models/user.py / UserRepository 契约对齐）：
    - 凭据 = 用户自己的百炼 API Key（本产品单凭据设计，无密码体系）；
    - users.api_key_hash = SHA-256(api_key)（唯一键，注册幂等判断）；
    - 明文 API Key 不落库：AES-256-GCM 密文 + 每条记录独立 nonce 落库，
      仅服务器以 APP_MASTER_KEY 解密后注入业务链路；
    - Bearer token 每次登录轮换，DB 只存 SHA-256(token)。

范围边界：
    - 不含密码 / 多因素 / OAuth —— 未来如需扩展，另开 Service 组合本类；
    - 不直接操作 Milvus / Document 表（那是 RAG 链路职责）；
    - 解密出的 API Key 明文只存在于调用栈内存：不写日志、不返回给客户端、
      不随任何响应体 / 异常消息泄露；
    - UserRepository 抛出的 UserOperationError → 包装为 AuthOperationError
      （503，与 DocumentOperationError → 503 风格对齐）。

异常契约：
    - ApiKeyInvalidError(401)           ：空 Key / 登录时未注册 / 用户被禁用；
    - ApiKeyAlreadyRegisteredError(409) ：注册时该 Key 已存在（应改走 login）；
    - AuthOperationError(503)           ：UserRepository 操作失败；
    - UserNotFoundError(404)            ：update_api_key 时 user_id 不存在（冒泡）；
    - SecurityConfigurationError(500)   ：APP_MASTER_KEY 非 32 bytes（冒泡）；
    - SecurityDecryptionError(500)      ：密文损坏 / 主密钥变更（冒泡）。
"""

from __future__ import annotations

from ..core.config import Settings
from ..core.exceptions import (
    ApiKeyAlreadyRegisteredError,
    ApiKeyInvalidError,
    AuthOperationError,
    UserOperationError,
)
from ..core.security import (
    decrypt_api_key,
    encrypt_api_key,
    generate_token,
    hash_token,
    sha256_hex,
)
from ..models.user import User, UserStatus
from ..repositories.mysql.user_protocol import UserRepository


class UserService:
    """
    用户认证业务编排：以「用户自己的百炼 API Key」为唯一身份凭据。

    实例持有 UserRepository（Protocol）与 APP_MASTER_KEY（Settings 注入），
    无状态、线程安全（所有安全计算为纯函数，所有 DB 操作委托 Repository）。
    """

    def __init__(self, user_repository: UserRepository, settings: Settings) -> None:
        """
        Args:
            user_repository: UserRepository（Protocol）；由 core.di 装配
                UserRepositoryImpl（生产）或测试 Mock。
            settings: Settings 单例；仅读取 app_master_key（AES-256-GCM 主密钥，
                必须 32 bytes，由 .env 的 APP_MASTER_KEY 注入）。
        """
        self._user_repository = user_repository
        self._master_key = settings.app_master_key

    # ---------------------------------------------------------------- register
    def register(self, api_key: str) -> tuple[User, str]:
        """
        注册新用户：新 API Key 创建用户并签发首个 Bearer token。

        流程：
            1) strip + 空值校验（空白 → ApiKeyInvalidError）；
            2) sha256(api_key) → 查 users；已存在 → ApiKeyAlreadyRegisteredError
               （提示改用 /auth/login；不泄露任何既有用户信息）；
            3) AES-256-GCM 加密 + 生成 opaque token → create_user
               （users.status 默认 ACTIVE）→ 返回 (User, token)。

        Args:
            api_key: 用户自己的百炼 API Key（明文仅存在于调用栈内存）。

        Returns:
            (User, token)：User 为已落库的 ORM 对象；token 为 opaque Bearer
            token（仅本次返回明文，DB 只存 hash）。

        Raises:
            ApiKeyInvalidError           : api_key 为空 / 全空白。
            ApiKeyAlreadyRegisteredError : 该 API Key 已注册（应改走 login）。
            AuthOperationError           : UserRepository 操作失败。
            SecurityConfigurationError   : APP_MASTER_KEY 非 32 bytes。
        """
        api_key = api_key.strip()
        if not api_key:
            raise ApiKeyInvalidError("api_key must not be empty")

        api_key_hash = sha256_hex(api_key)
        existing = self._user_repository.get_user_by_api_key_hash(api_key_hash)
        if existing is not None:
            raise ApiKeyAlreadyRegisteredError(
                "api key already registered; please use /auth/login instead"
            )

        ciphertext, nonce = encrypt_api_key(api_key, self._master_key)
        token = generate_token()
        try:
            user = self._user_repository.create_user(
                api_key_hash=api_key_hash,
                api_key_ciphertext=ciphertext,
                api_key_nonce=nonce,
                token_hash=hash_token(token),
            )
        except UserOperationError as e:
            raise AuthOperationError(f"register failed: {e}") from e
        return user, token

    # ------------------------------------------------------------------- login
    def login(self, api_key: str) -> tuple[User, str]:
        """
        登录：已注册用户用 API Key 换取 token，每次登录轮换（旧 token 立即失效）。

        流程：
            1) strip + 空值校验；
            2) sha256(api_key) → 查 users；不存在 → ApiKeyInvalidError
               （提示先注册；不泄露用户是否存在）；
            3) user.status != ACTIVE（DISABLED）→ ApiKeyInvalidError；
            4) 生成新 token → update_token 轮换（旧 token 失效）→ 返回 (User, token)。

        Args:
            api_key: 用户自己的百炼 API Key。

        Returns:
            (User, token)：token 为本次登录新签发的明文（仅返回一次）。

        Raises:
            ApiKeyInvalidError : api_key 未注册 / 用户被禁用。
            AuthOperationError : UserRepository 操作失败。
        """
        api_key = api_key.strip()
        if not api_key:
            raise ApiKeyInvalidError("api_key must not be empty")

        api_key_hash = sha256_hex(api_key)
        user = self._user_repository.get_user_by_api_key_hash(api_key_hash)
        if user is None:
            raise ApiKeyInvalidError(
                "api key not registered; please call /auth/register first"
            )
        if user.status != UserStatus.ACTIVE:
            raise ApiKeyInvalidError("user is disabled")

        token = generate_token()
        try:
            self._user_repository.update_token(user.id, hash_token(token))
        except UserOperationError as e:
            raise AuthOperationError(f"login failed: {e}") from e
        return user, token

    # ----------------------------------------------------------- update_api_key
    def update_api_key(self, user_id: int, api_key: str) -> User:
        """
        更换用户自己的 API Key（token / user_id / 知识库归属不变）。

        流程：
            1) strip + 空值校验；
            2) AES-256-GCM 加密 + sha256 → repo.update_api_key
               （只更新 api_key_hash / ciphertext / nonce + updated_at；
               token_hash 不变，客户端可继续使用原 token）。

        Args:
            user_id: 当前登录用户主键（来自 get_current_user）。
            api_key: 新的百炼 API Key。

        Returns:
            更新后的 User ORM 对象。

        Raises:
            ApiKeyInvalidError : api_key 为空。
            UserNotFoundError  : user_id 不存在（冒泡 → 404）。
            AuthOperationError : UserRepository 操作失败。
            SecurityConfigurationError : APP_MASTER_KEY 非 32 bytes。
        """
        api_key = api_key.strip()
        if not api_key:
            raise ApiKeyInvalidError("api_key must not be empty")

        ciphertext, nonce = encrypt_api_key(api_key, self._master_key)
        try:
            return self._user_repository.update_api_key(
                user_id=user_id,
                api_key_hash=sha256_hex(api_key),
                api_key_ciphertext=ciphertext,
                api_key_nonce=nonce,
            )
        except UserOperationError as e:
            raise AuthOperationError(f"update_api_key failed: {e}") from e

    # ---------------------------------------------------------- decrypt_api_key
    def decrypt_api_key(self, user: User) -> str:
        """
        解密用户的 API Key 明文，供 Embedding / LLM 业务链路注入。

        调用方（Router）拿到明文后应立即传给下游 Client（embed / generate），
        不得写日志、不得返回给客户端、不得跨请求缓存。

        Args:
            user: 当前登录 User ORM 对象（含 api_key_ciphertext / nonce）。

        Returns:
            API Key 明文（仅存在于调用栈内存）。

        Raises:
            SecurityConfigurationError : APP_MASTER_KEY 非 32 bytes。
            SecurityDecryptionError    : 密文 / nonce 损坏或主密钥变更（500）。
        """
        return decrypt_api_key(
            user.api_key_ciphertext,
            user.api_key_nonce,
            self._master_key,
        )
