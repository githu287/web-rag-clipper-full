"""
Plugin Workspace 身份体系业务编排服务（Phase 3.5 Step 2-C 新增）。

职责：
    register / authenticate / update_api_key / decrypt_api_key /
    remove_api_key / update_plugin_name / delete_workspace / get_plugin
    —— 即「Workspace 注册、凭证认证、API Key 配置、改名、删除前置确认、
    基本查询」这一层的全部业务编排。

最终链路：
    plugin_name
      ↓ PluginService.register()
      ↓ plugin_id + plugin_secret
      ↓ plugin_secret_hash → DB
      ↓ get_current_plugin()（Step 2-D）
      ↓ PluginWorkspace

分层边界（严格）：
    - 不直接操作 SQL（全部经 PluginRepository）；
    - 不负责加密 / hash / 随机生成（那是 core.security 职责）；
    - 不负责 HTTP 语义（那是 Router + 异常 handler 职责）；
    - API Key 永远不参与 Workspace 认证：身份判定仅依赖
      plugin_id + plugin_secret → plugin_secret_hash。

身份职责边界：
    plugin_id            = 标识（明文存 DB / X-Plugin-ID header，非凭证）
    plugin_secret        = 身份认证凭证（SHA-256 后仅存 hash）
    plugin_secret_hash   = 认证比对目标（SHA-256，hmac.compare_digest 恒时比较）
    api_key_ciphertext/nonce = 百炼模型调用凭证（AES-256-GCM 加密副本）

安全红线：
    - plugin_secret 明文只存在于 register() 返回值（本次调用内存），
      绝不写入数据库 / 日志 / 异常消息 / Repository 参数；
    - 解密后的 API Key 明文只存在于调用栈内存：不写日志、不落库、
      不写 Redis、不进 response、不进异常消息；
    - 认证失败 / 异常消息禁止回显 plugin_id / secret / hash；
    - 更新 API Key 只使用「用户提交的 Key」，绝不 fallback 到
      settings.bailian_api_key。

依赖注入：
    - plugin_repository: PluginRepository（core.di.get_plugin_repository）
    - settings          : Settings（core.di.get_settings；提供 APP_MASTER_KEY）
    - embedding_client  : EmbeddingClient | None（core.di.get_embedding_client；
      仅用于 update_api_key 的最小验证；可为 None 以禁用验证，便于测试）
"""

from __future__ import annotations

import hmac
import logging
import re
from typing import Final

from ..clients.embedding import EmbeddingClient
from ..core.config import Settings
from ..core.exceptions import (
    ApiKeyNotConfiguredError,
    ApiKeyValidationError,
    PluginCredentialsMissingError,
    PluginDeleteConfirmationError,
    PluginDisabledError,
    PluginNameTakenError,
    PluginNameValidationError,
    PluginNotFoundError,
    PluginOperationError,
    PluginSecretMismatchError,
)
from ..core.security import (
    decrypt_api_key as _decrypt_ciphertext,
    encrypt_api_key,
    generate_plugin_id,
    generate_plugin_secret,
    hash_plugin_secret,
)
from ..models.plugin import PluginStatus, PluginWorkspace
from ..repositories.mysql.plugin_protocol import PluginRepository

logger: logging.Logger = logging.getLogger(__name__)

# update_api_key 最小验证用的探针文本（单条最小 embedding 请求，不调 LLM；
# 复用现有 EmbeddingClient，不创建第二套 HTTP Client）
_API_KEY_VALIDATION_PROBE: Final[str] = "api-key-validation-probe"

# plugin_name 规则（Phase 3.5 §7）：
#   - trim 后长度 2 ≤ n ≤ 32；
#   - 允许中文 / 英文字母 / 数字 / 空格 / - / _ / .；
#   - 禁止换行、tab、控制字符及其他特殊符号。
_PLUGIN_NAME_MIN_LENGTH: Final[int] = 2
_PLUGIN_NAME_MAX_LENGTH: Final[int] = 32
_PLUGIN_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-zA-Z0-9\u4e00-\u9fff _.\-]+$"
)


def validate_plugin_name(name: str) -> str:
    """校验 plugin_name，返回 trim 后的显示名；非法抛 PluginNameValidationError。

    规则（Phase 3.5 §7）：
        - 非空（trim 后）；
        - 禁止控制字符（换行 / tab / 其他 Cc/Cf 类，用 str.isprintable 判定）；
        - 仅允许中文 / 英文字母 / 数字 / 空格 / - / _ / .；
        - trim 后长度 2 ≤ n ≤ 32。

    返回 strip 后的原始显示名（保留大小写与内部格式），归一化由
    normalize_plugin_name 单独负责。

    Raises:
        PluginNameValidationError: 空值 / 控制字符 / 非法字符集 / 长度越界。
    """
    if not isinstance(name, str) or not name.strip():
        raise PluginNameValidationError("plugin name is required")
    stripped = name.strip()
    if any(not ch.isprintable() for ch in stripped):
        raise PluginNameValidationError(
            "plugin name must not contain control characters"
        )
    if not _PLUGIN_NAME_PATTERN.match(stripped):
        raise PluginNameValidationError(
            "plugin name can only contain Chinese/English letters, digits, "
            "spaces, '-', '_', '.'"
        )
    if not (_PLUGIN_NAME_MIN_LENGTH <= len(stripped) <= _PLUGIN_NAME_MAX_LENGTH):
        raise PluginNameValidationError(
            f"plugin name must be {_PLUGIN_NAME_MIN_LENGTH}-"
            f"{_PLUGIN_NAME_MAX_LENGTH} characters"
        )
    return stripped


def normalize_plugin_name(name: str) -> str:
    """归一化 plugin_name：strip → 连续空白压缩为一个空格 → lower。

    示例：
        "  My AI Plugin  " → "my ai plugin"
        "My   AI   Plugin" → "my ai plugin"
        "我的 AI 助手"      → "我的 ai 助手"
        "我的AI助手"        → "我的ai助手"

    归一化名用于唯一性查重（plugin_name_norm 列），不用于展示。
    """
    return " ".join(name.split()).lower()


class PluginService:
    """Plugin Workspace 注册 / 认证 / API Key / 改名 / 删除 / 查询的业务编排服务。"""

    def __init__(
        self,
        plugin_repository: PluginRepository,
        settings: Settings,
        embedding_client: EmbeddingClient | None = None,
    ) -> None:
        """
        构造 PluginService。

        Args:
            plugin_repository: plugin_workspaces 表数据访问（core.di.get_plugin_repository）。
            settings: 配置单源；仅使用 app_master_key（API Key 加密）。
            embedding_client: 可选；update_api_key 时用用户提交的 Key
                调最小 embedding 请求做真实验证（不调 LLM）。
        """
        self._plugin_repository: PluginRepository = plugin_repository
        self._master_key: str = settings.app_master_key
        self._embedding_client: EmbeddingClient | None = embedding_client

    # ------------------------------------------------------------------ register
    def register(self, plugin_name: str) -> tuple[PluginWorkspace, str]:
        """
        注册新 Plugin Workspace（仅 plugin_name，返回 plugin_id + plugin_secret）。

        流程：
            1. validate_plugin_name（PluginNameValidationError → 422/400）；
            2. normalize_plugin_name → norm；
            3. get_by_plugin_name_norm 查重（已存在 → PluginNameTakenError → 409，
               先查再插入，不依赖数据库 IntegrityError 作为唯一判断）；
            4. 后端生成 plugin_id（secrets.token_urlsafe(32)，约 43 字符）；
            5. 后端生成 plugin_secret（secrets.token_urlsafe(32)，约 43 字符）；
            6. sha256(plugin_secret) → plugin_secret_hash；
            7. create_plugin(plugin_id, plugin_name, plugin_name_norm,
               plugin_secret_hash)；
            8. 返回 (workspace, plugin_secret)。

        安全要求：
            - plugin_secret 明文只出现在返回值中，一次性交给 Router；
            - 不持久化 / 不打印 / 不记录 plugin_secret；
            - Repository 只接收 plugin_secret_hash。

        Args:
            plugin_name: Workspace 显示名（2-32 字符，规则见 validate_plugin_name）。

        Returns:
            (workspace, plugin_secret)：新建 PluginWorkspace + 明文 plugin_secret
            （仅本次返回值；调用方必须立即交付给注册者）。

        Raises:
            PluginNameValidationError: 名称非法（422/400）。
            PluginNameTakenError: 归一化名已被占用（409）。
            PluginOperationError: plugin_workspaces 表写入异常（503）。
        """
        validated = validate_plugin_name(plugin_name)
        norm = normalize_plugin_name(validated)
        existing = self._plugin_repository.get_by_plugin_name_norm(norm)
        if existing is not None:
            raise PluginNameTakenError(f"plugin name already taken: {norm!r}")
        plugin_id = generate_plugin_id()
        plugin_secret = generate_plugin_secret()
        secret_hash = hash_plugin_secret(plugin_secret)
        try:
            workspace = self._plugin_repository.create_plugin(
                plugin_id=plugin_id,
                plugin_name=validated,
                plugin_name_norm=norm,
                plugin_secret_hash=secret_hash,
            )
        except PluginOperationError:
            logger.warning("register failed: plugin_id=%s", plugin_id)
            raise
        logger.info("register success: plugin_id=%s", plugin_id)
        return workspace, plugin_secret

    # ------------------------------------------------------------- authenticate
    def authenticate(self, plugin_id: str, plugin_secret: str) -> PluginWorkspace:
        """
        校验 Plugin 凭证（plugin_id + plugin_secret → PluginWorkspace）。

        流程：
            1. plugin_id 缺失 → PluginCredentialsMissingError（401）；
            2. plugin_secret 缺失 → PluginCredentialsMissingError（401）；
            3. get_by_plugin_id(plugin_id)，不存在 → PluginNotFoundError（401）；
            4. sha256(plugin_secret) 后 hmac.compare_digest 与
               plugin_secret_hash 恒时比较，不匹配 → PluginSecretMismatchError（401）；
            5. status != ACTIVE → PluginDisabledError（403）；
            6. 返回 PluginWorkspace（供 get_current_plugin / 业务 Service 使用）。

        安全要求：
            - 禁止 `if actual_hash == expected_hash:`，必须 hmac.compare_digest；
            - 错误消息禁止回显 plugin_id / secret / hash。

        Args:
            plugin_id: X-Plugin-ID 请求头（标识，明文）。
            plugin_secret: X-Plugin-Secret 请求头（认证凭证，仅调用栈内存）。

        Returns:
            认证通过的 PluginWorkspace ORM 对象。

        Raises:
            PluginCredentialsMissingError: 凭据缺失（401）。
            PluginNotFoundError: plugin_id 不存在（401）。
            PluginSecretMismatchError: secret 不匹配（401）。
            PluginDisabledError: workspace 已禁用（403）。
        """
        if not plugin_id or not plugin_secret:
            raise PluginCredentialsMissingError("plugin credentials are missing")
        workspace = self._plugin_repository.get_by_plugin_id(plugin_id)
        if workspace is None:
            raise PluginNotFoundError("plugin not found")
        if not hmac.compare_digest(
            hash_plugin_secret(plugin_secret), workspace.plugin_secret_hash
        ):
            raise PluginSecretMismatchError("plugin secret mismatch")
        if workspace.status != PluginStatus.ACTIVE:
            raise PluginDisabledError("plugin is disabled")
        logger.info("authenticate success: plugin_id=%s", plugin_id)
        return workspace

    # ------------------------------------------------------------- update_api_key
    def update_api_key(self, plugin_id: str, api_key: str) -> PluginWorkspace:
        """
        配置 / 更换 Workspace 的百炼 API Key（AES-256-GCM 加密入库）。

        流程：
            1. strip + 空值防御（ApiKeyValidationError → 400）；
            2. sk- 前缀防御（ApiKeyValidationError → 400）；
            3. 注入的 EmbeddingClient 用「用户提交的 Key」做最小 embedding
               验证（不调 LLM）；失败 → ApiKeyValidationError → 400；
            4. encrypt_api_key(api_key, APP_MASTER_KEY) → ciphertext + nonce
               （APP_MASTER_KEY 缺失/非法 → SecurityConfigurationError）；
            5. PluginRepository.update_api_key(plugin_id, ciphertext, nonce)。

        明确不改变：plugin_id / plugin_name / plugin_name_norm /
        plugin_secret_hash / status / documents / Milvus。

        安全要求：
            - 只使用用户提交的 api_key，绝不 fallback settings.bailian_api_key；
            - 明文 api_key 不写日志 / 不落库 / 不进异常消息。

        Args:
            plugin_id: Workspace 标识。
            api_key: 用户自己的百炼 API Key（sk- 开头；仅调用栈内存）。

        Returns:
            更新后的 PluginWorkspace ORM 对象（detached 可读）。

        Raises:
            ApiKeyValidationError: Key 为空 / 非 sk- 前缀 / 最小 embedding 验证失败（400）。
            PluginNotFoundError: plugin_id 不存在（401）。
            SecurityConfigurationError: APP_MASTER_KEY 非 32 bytes（503）。
            PluginOperationError: plugin_workspaces 表写入异常（503）。
        """
        api_key = (api_key or "").strip()
        if not api_key:
            raise ApiKeyValidationError("api_key must not be empty")
        if not api_key.startswith("sk-"):
            raise ApiKeyValidationError("api_key must start with 'sk-'")
        if self._embedding_client is not None:
            try:
                self._embedding_client.embed(
                    [_API_KEY_VALIDATION_PROBE], api_key=api_key
                )
            except Exception as e:  # noqa: BLE001 — 所有百炼侧异常视为 Key 无效
                logger.warning(
                    "api_key validation failed: plugin_id=%s, error_type=%s",
                    plugin_id,
                    type(e).__name__,
                )
                raise ApiKeyValidationError(
                    "API Key 验证失败：请检查 Key 是否有效。"
                ) from e
        ciphertext, nonce = encrypt_api_key(api_key, self._master_key)
        try:
            workspace = self._plugin_repository.update_api_key(
                plugin_id, ciphertext, nonce
            )
        except PluginNotFoundError:
            raise
        except PluginOperationError:
            logger.warning("update_api_key failed: plugin_id=%s", plugin_id)
            raise
        logger.info("update_api_key success: plugin_id=%s", plugin_id)
        return workspace

    # ------------------------------------------------------------ decrypt_api_key
    def decrypt_api_key(self, plugin: PluginWorkspace) -> str:
        """
        解密 Workspace 的百炼 API Key（仅业务链路 Embedding / LLM 使用）。

        规则：
            - ciphertext 或 nonce 任一为 None → ApiKeyNotConfiguredError（409）；
            - 不 fallback 到 settings.bailian_api_key（.env 的 Key 仅用于
              开发 / migration / 测试，不能作为已认证真实 Workspace 的业务凭据）；
            - 解密失败（密文损坏 / 主密钥更换）→ SecurityDecryptionError。

        Args:
            plugin: 认证通过的 PluginWorkspace（必须含 api_key_ciphertext /
                api_key_nonce）。

        Returns:
            明文 API Key（仅调用栈内存；调用方不得写日志 / 落库 / 进 response）。

        Raises:
            ApiKeyNotConfiguredError: Workspace 尚未配置 API Key（409）。
            SecurityDecryptionError: 解密失败（500）。
        """
        if plugin.api_key_ciphertext is None or plugin.api_key_nonce is None:
            raise ApiKeyNotConfiguredError(
                "当前 Workspace 尚未配置阿里云百炼 API Key，请前往设置配置。"
            )
        return _decrypt_ciphertext(
            plugin.api_key_ciphertext, plugin.api_key_nonce, self._master_key
        )

    # ------------------------------------------------------------- remove_api_key
    def remove_api_key(self, plugin_id: str) -> PluginWorkspace:
        """
        清除 Workspace 的百炼 API Key（api_key_ciphertext / nonce → NULL）。

        明确不改变：plugin_id / plugin_name / plugin_name_norm /
        plugin_secret_hash / status；不删除 documents、不修改任何知识库数据。

        Args:
            plugin_id: Workspace 标识。

        Returns:
            更新后的 PluginWorkspace ORM 对象（api_key_* 为 None）。

        Raises:
            PluginNotFoundError: plugin_id 不存在（401）。
            PluginOperationError: plugin_workspaces 表写入异常（503）。
        """
        try:
            workspace = self._plugin_repository.clear_api_key(plugin_id)
        except PluginNotFoundError:
            raise
        except PluginOperationError:
            logger.warning("remove_api_key failed: plugin_id=%s", plugin_id)
            raise
        logger.info("remove_api_key success: plugin_id=%s", plugin_id)
        return workspace

    # -------------------------------------------------------- update_plugin_name
    def update_plugin_name(self, plugin_id: str, plugin_name: str) -> PluginWorkspace:
        """
        修改 Workspace 显示名（plugin_id 不变）。

        流程：
            validate → normalize → 查重（命中其他 workspace →
            PluginNameTakenError）→ repository.update_plugin_name。

        必须保证：plugin_id / plugin_secret_hash / api_key_ciphertext /
        api_key_nonce / documents 均不变。

        Args:
            plugin_id: Workspace 标识。
            plugin_name: 新显示名（规则见 validate_plugin_name）。

        Returns:
            更新后的 PluginWorkspace ORM 对象。

        Raises:
            PluginNotFoundError: plugin_id 不存在（401）。
            PluginNameValidationError: 名称非法（422/400）。
            PluginNameTakenError: 新名称被其他 workspace 占用（409）。
            PluginOperationError: plugin_workspaces 表写入异常（503）。
        """
        validated = validate_plugin_name(plugin_name)
        norm = normalize_plugin_name(validated)
        # 前置存在性校验（不存在 → PluginNotFoundError）
        self.get_plugin(plugin_id)
        existing = self._plugin_repository.get_by_plugin_name_norm(norm)
        if existing is not None and existing.plugin_id != plugin_id:
            raise PluginNameTakenError(f"plugin name already taken: {norm!r}")
        try:
            updated = self._plugin_repository.update_plugin_name(
                plugin_id, validated, norm
            )
        except PluginOperationError:
            logger.warning("update_plugin_name failed: plugin_id=%s", plugin_id)
            raise
        logger.info("update_plugin_name success: plugin_id=%s", plugin_id)
        return updated

    # --------------------------------------------------------- delete_workspace
    def delete_workspace(
        self, plugin_id: str, confirm: bool, plugin_name: str
    ) -> None:
        """
        Workspace 数据库删除前置校验 + Repository 删除（Service 层）。

        前置校验：
            - confirm is False → PluginDeleteConfirmationError（400）；
            - plugin_name != 当前 workspace 显示名 → PluginDeleteConfirmationError（400）。

        通过后：PluginRepository.delete_plugin(plugin_id)。

        重要：本阶段只删除 plugin_workspaces 行；documents → Milvus →
        FileStorage → workspace 的级联删除属于后续业务 Service / Router 阶段，
        本方法不触碰 Documents / Milvus / 文件存储。

        Args:
            plugin_id: Workspace 标识。
            confirm: 必须为 True。
            plugin_name: 必须与当前 workspace 显示名完全一致。

        Raises:
            PluginNotFoundError: plugin_id 不存在（401）。
            PluginDeleteConfirmationError: confirm 缺失或 plugin_name 不匹配（400）。
            PluginOperationError: plugin_workspaces 表删除异常（503）。
        """
        workspace = self.get_plugin(plugin_id)
        if not confirm:
            raise PluginDeleteConfirmationError(
                "workspace deletion requires confirm=True"
            )
        if plugin_name != workspace.plugin_name:
            raise PluginDeleteConfirmationError(
                "plugin name does not match the current workspace name"
            )
        try:
            self._plugin_repository.delete_plugin(plugin_id)
        except PluginOperationError:
            logger.warning("delete_workspace failed: plugin_id=%s", plugin_id)
            raise
        logger.info("delete_workspace success: plugin_id=%s", plugin_id)

    # ---------------------------------------------------------------- get_plugin
    def get_plugin(self, plugin_id: str) -> PluginWorkspace:
        """
        按 plugin_id 获取 Plugin Workspace（不存在 → PluginNotFoundError）。

        用于后续：get_current_plugin（Step 2-D）/ GET /plugins/me。

        Args:
            plugin_id: Workspace 标识。

        Returns:
            PluginWorkspace ORM 对象。

        Raises:
            PluginNotFoundError: plugin_id 不存在（401）。
        """
        workspace = self._plugin_repository.get_by_plugin_id(plugin_id)
        if workspace is None:
            raise PluginNotFoundError("plugin not found")
        return workspace
