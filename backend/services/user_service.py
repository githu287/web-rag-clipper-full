"""
用户 / 认证业务编排服务（Phase 3.4 Step 3 用户体系；F-REV3 身份重构）。

职责：
    register / login / logout / update_api_key / remove_api_key /
    decrypt_api_key / get_current_user —— 即「用户注册、登录、会话、
    API Key 配置」这一层的全部业务编排。

分层边界（严格）：
    - 不直接操作 SQL（全部经 UserRepository）；
    - 不负责加密 / hash / token 生成（那是 core.security 职责）；
    - 不负责 HTTP 语义（那是 Router + 异常 handler 职责）；
    - API Key 永远不参与身份认证：本服务所有身份判定仅依赖
      username → users.id → token_hash。

身份职责边界（F-REV3 最终版）：
    username        = 身份入口（UNIQUE NOT NULL）
    users.id        = 系统内部稳定身份主键
    password_hash   = 登录凭证（Argon2id）
    token_hash      = 会话认证（Bearer token 的 SHA-256）
    api_key_ciphertext/nonce = 百炼模型调用凭证（AES-256-GCM 加密副本）

安全红线：
    - 本模块可接触 password / token 明文（仅调用栈内存），
      绝不写入日志 / 数据库 / 异常消息；
    - 不输出 API Key 明文、ciphertext 完整内容、token 明文、
      APP_MASTER_KEY 到任何对外信息。

依赖注入：
    - user_repository: UserRepository（core.di.get_user_repository）
    - settings       : Settings（core.di.get_settings；提供 APP_MASTER_KEY）
    - embedding_client: EmbeddingClient | None（core.di.get_embedding_client；
      仅用于 update_api_key 的最小验证；可为 None 以禁用验证，便于测试）
"""

from __future__ import annotations

import logging

from ..clients.embedding import EmbeddingClient
from ..core.config import Settings
from ..core.exceptions import (
    ApiKeyNotConfiguredError,
    ApiKeyValidationError,
    AuthOperationError,
    DisabledUserError,
    InvalidCredentialsError,
    PasswordPolicyError,
    UsernameAlreadyExistsError,
    UserNotFoundError,
    UserOperationError,
)
from ..core.security import (
    decrypt_api_key as _decrypt_ciphertext,
    encrypt_api_key,
    generate_token,
    hash_password,
    hash_token,
    validate_password_strength,
    verify_password,
)
from ..models.user import User, UserStatus
from ..repositories.mysql.user_protocol import UserRepository

logger: logging.Logger = logging.getLogger(__name__)

# update_api_key 最小验证用的探针文本（单条最小 embedding 请求，不调 LLM）
_API_KEY_VALIDATION_PROBE: str = "api-key-validation-probe"


class UserService:
    """用户注册 / 登录 / 会话 / API Key 配置的业务编排服务。"""

    def __init__(
        self,
        user_repository: UserRepository,
        settings: Settings,
        embedding_client: EmbeddingClient | None = None,
    ) -> None:
        """
        构造 UserService。

        Args:
            user_repository: users 表数据访问（core.di.get_user_repository）。
            settings: 配置单源；仅使用 app_master_key（API Key 加密）。
            embedding_client: 可选；update_api_key 时用用户提交的 Key
                调最小 embedding 请求做真实验证（不调 LLM）。
        """
        self._user_repository: UserRepository = user_repository
        self._master_key: str = settings.app_master_key
        self._embedding_client: EmbeddingClient | None = embedding_client

    # ------------------------------------------------------------------ register
    def register(self, username: str, password: str) -> tuple[User, str]:
        """
        注册新用户（仅 username + password，完全脱离第三方服务）。

        流程：
            1. strip + 校验 password 强度（PasswordPolicyError → 422）；
            2. 查重 username（已存在 → UsernameAlreadyExistsError → 409）；
            3. password → Argon2id password_hash；
            4. generate_token + hash_token；
            5. create_user(username, password_hash, token_hash,
                           api_key_ciphertext=None, api_key_nonce=None)；
            6. 返回 (User, token)。

        明确不执行：调百炼 / 校验 API Key / 加密 API Key / Embedding / LLM。
        注册成功即已登录（token 可直接认证）。

        Args:
            username: 身份入口（1-64 字符）。
            password: 明文密码（仅调用栈内存；8-128 字符）。

        Returns:
            (User, token)：新建的 User ORM 对象 + opaque Bearer token。

        Raises:
            UsernameAlreadyExistsError: username 已被注册（409）。
            PasswordPolicyError: 密码长度 / 复杂度不满足（422）。
            AuthOperationError: users 表写入异常（503）。
        """
        username = (username or "").strip()
        # 先校验密码强度：非法密码不触发 DB 查询，避免暴露 username 存在性
        validate_password_strength(password)
        existing = self._user_repository.get_user_by_username(username)
        if existing is not None:
            raise UsernameAlreadyExistsError(
                f"username already exists: {username!r}"
            )
        password_hash = hash_password(password)
        token = generate_token()
        try:
            user = self._user_repository.create_user(
                username=username,
                password_hash=password_hash,
                token_hash=hash_token(token),
                api_key_ciphertext=None,
                api_key_nonce=None,
            )
        except UserOperationError as e:
            # 唯一约束竞态（并发重复注册）也包装为 409 语义
            raise AuthOperationError(
                f"register failed: username={username!r}, error={e}"
            ) from e
        logger.info("register success: username=%s, user_id=%s", username, user.id)
        return user, token

    # ----------------------------------------------------------------------- login
    def login(self, username: str, password: str) -> tuple[User, str]:
        """
        登录（仅 username + password；API Key 完全不参与）。

        流程：
            1. get_user_by_username；
            2. 不存在 → InvalidCredentialsError（401，与密码错误同语义）；
            3. verify_password 失败 → InvalidCredentialsError（401）；
            4. status != ACTIVE → DisabledUserError（403）；
            5. generate_token + hash_token + update_token（轮换，旧 token 失效）；
            6. 返回 (User, token)。

        安全要求：
            - 用户不存在与密码错误返回完全相同的异常类型与消息语义
              （防用户枚举）；
            - 登录过程绝不读取 / 解密 API Key，绝不调百炼。

        Args:
            username: 身份入口。
            password: 明文密码（仅调用栈内存）。

        Returns:
            (User, token)：登录用户 + 新签发的 opaque Bearer token。

        Raises:
            InvalidCredentialsError: username 不存在或密码错误（401）。
            DisabledUserError: 用户被禁用（403）。
            AuthOperationError: users 表写入异常（503）。
        """
        username = (username or "").strip()
        user = self._user_repository.get_user_by_username(username)
        if user is None:
            raise InvalidCredentialsError("invalid username or password")
        if not verify_password(password, user.password_hash):
            raise InvalidCredentialsError("invalid username or password")
        if user.status != UserStatus.ACTIVE:
            raise DisabledUserError("user is disabled")
        token = generate_token()
        try:
            self._user_repository.update_token(user.id, hash_token(token))
        except UserOperationError as e:
            raise AuthOperationError(
                f"login failed: user_id={user.id}, error={e}"
            ) from e
        logger.info("login success: username=%s, user_id=%s", username, user.id)
        return user, token

    # ---------------------------------------------------------------------- logout
    def logout(self, user_id: int) -> None:
        """
        登出：清除用户会话 token（旧 token 立即 401）。

        流程：clear_token(user_id) → token_hash = NULL。
        不创建 refresh token、不引入 Redis。

        Args:
            user_id: 已认证用户主键（来自 get_current_user）。

        Raises:
            UserNotFoundError: user_id 不存在（防御；get_current_user 已保证存在）。
            AuthOperationError: users 表写入异常（503）。
        """
        try:
            self._user_repository.clear_token(user_id)
        except UserNotFoundError:
            raise
        except UserOperationError as e:
            raise AuthOperationError(
                f"logout failed: user_id={user_id}, error={e}"
            ) from e
        logger.info("logout success: user_id=%s", user_id)

    # ------------------------------------------------------------- update_api_key
    def update_api_key(self, user_id: int, api_key: str) -> User:
        """
        配置 / 更换用户的百炼 API Key（仅通过 users.id 关联，不参与身份）。

        流程：
            1. strip + 空值防御（ApiKeyValidationError → 400）；
            2. 注入的 EmbeddingClient 用「用户提交的 Key」做最小 embedding
               验证（不调 LLM）；失败 → ApiKeyValidationError → 400；
            3. encrypt_api_key(api_key, APP_MASTER_KEY) → ciphertext + nonce；
            4. UserRepository.update_api_key(user_id, ciphertext, nonce)。

        明确不改变：user_id / username / password_hash / token_hash /
        status / documents 归属。

        Args:
            user_id: 已认证用户主键。
            api_key: 用户自己的百炼 API Key（sk- 开头；仅调用栈内存）。

        Returns:
            更新后的 User ORM 对象（detached 可读）。

        Raises:
            ApiKeyValidationError: Key 为空或最小 embedding 验证失败（400）。
            UserNotFoundError: user_id 不存在（404）。
            AuthOperationError: 加密配置异常 / users 表写入异常（503）。
        """
        api_key = (api_key or "").strip()
        if not api_key:
            raise ApiKeyValidationError("api_key must not be empty")
        if self._embedding_client is not None:
            try:
                self._embedding_client.embed(
                    [_API_KEY_VALIDATION_PROBE], api_key=api_key
                )
            except Exception as e:  # noqa: BLE001 — 所有百炼侧异常视为 Key 无效
                logger.warning(
                    "api_key validation failed: user_id=%s, error_type=%s",
                    user_id,
                    type(e).__name__,
                )
                raise ApiKeyValidationError(
                    "API Key 验证失败：请检查 Key 是否有效。"
                ) from e
        ciphertext, nonce = encrypt_api_key(api_key, self._master_key)
        try:
            user = self._user_repository.update_api_key(
                user_id, ciphertext, nonce
            )
        except UserNotFoundError:
            raise
        except UserOperationError as e:
            raise AuthOperationError(
                f"update_api_key failed: user_id={user_id}, error={e}"
            ) from e
        logger.info("update_api_key success: user_id=%s", user_id)
        return user

    # ------------------------------------------------------------ remove_api_key
    def remove_api_key(self, user_id: int) -> User:
        """
        清除用户的百炼 API Key（api_key_ciphertext / nonce → NULL）。

        明确不改变：user_id / username / password_hash / token_hash /
        status；不删除用户、不删除 documents、不修改任何知识库数据。

        Args:
            user_id: 已认证用户主键。

        Returns:
            更新后的 User ORM 对象（api_key_* 为 None）。

        Raises:
            UserNotFoundError: user_id 不存在（404）。
            AuthOperationError: users 表写入异常（503）。
        """
        try:
            user = self._user_repository.clear_api_key(user_id)
        except UserNotFoundError:
            raise
        except UserOperationError as e:
            raise AuthOperationError(
                f"remove_api_key failed: user_id={user_id}, error={e}"
            ) from e
        logger.info("remove_api_key success: user_id=%s", user_id)
        return user

    # ------------------------------------------------------------ decrypt_api_key
    def decrypt_api_key(self, user: User) -> str:
        """
        解密用户的百炼 API Key（仅业务链路 Embedding / LLM 使用）。

        规则：
            - ciphertext 或 nonce 任一为 None → ApiKeyNotConfiguredError（409）；
            - 不 fallback 到 settings.bailian_api_key（.env 的 Key 仅用于
              开发 / migration / 测试，不能作为已认证真实用户的业务凭据）；
            - 解密失败（密文损坏 / 主密钥更换）→ SecurityDecryptionError。

        Args:
            user: 已认证用户（必须含 api_key_ciphertext / api_key_nonce）。

        Returns:
            明文 API Key（仅调用栈内存；调用方不得写日志 / 落库）。

        Raises:
            ApiKeyNotConfiguredError: 账号尚未配置 API Key（409）。
            SecurityDecryptionError: 解密失败（500）。
        """
        if user.api_key_ciphertext is None or user.api_key_nonce is None:
            raise ApiKeyNotConfiguredError(
                "当前账号尚未配置阿里云百炼 API Key，请前往设置配置。"
            )
        return _decrypt_ciphertext(
            user.api_key_ciphertext, user.api_key_nonce, self._master_key
        )

    # ---------------------------------------------------------- get_current_user
    def get_current_user(self, user_id: int) -> User:
        """
        按主键获取当前用户（GET /users/me 使用）。

        Args:
            user_id: 来自 get_current_user 依赖解析的已认证用户主键。

        Returns:
            User ORM 对象。

        Raises:
            UserNotFoundError: user_id 不存在（404；防御兜底）。
        """
        user = self._user_repository.get_user_by_id(user_id)
        if user is None:
            raise UserNotFoundError(f"user not found: id={user_id}")
        return user
