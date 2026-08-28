"""
PluginService 专项测试（Phase 3.5 Step 2-C）。

覆盖范围（对应 §24 测试要求）：
    Register：成功 / plugin_id 自动生成 / plugin_secret 自动生成 /
              secret 不进 repository 明文参数 / secret_hash 正确 / 重复 plugin_name /
              名称归一化 / 非法名称；
    Authenticate：正常认证 / plugin_id 不存在 / secret 错误 / disabled workspace /
                  缺 plugin_id / 缺 plugin_secret；
    API Key：正常配置 / 配置后能正确解密 / 未配置 Key → ApiKeyNotConfiguredError /
              删除 Key / 删除后仍保持 plugin_id / 删除后仍保持 secret_hash；
    修改名称：改名 / 重名失败 / 改名后 plugin_id 不变 / secret_hash 不变 / API Key 不变；
    删除 Workspace：confirm=False → 失败 / plugin_name 不匹配 → 失败 /
                     正确 confirm → Repository.delete_plugin；
    安全：secret 不写日志 / api_key 不写日志 / repository 不接收 plugin_secret 明文 /
          plugin_secret 使用 compare_digest 验证。

测试策略：
    - 注入 Mock PluginRepository（不连真实 MySQL）；
    - 注入 Mock EmbeddingClient（不调真实百炼）；
    - secret hash 用真实 core.security；AES 加密 / 解密验证用 patch 隔离
      外部副作用，个别用例用真实 32 bytes master key 验证加解密闭环。

不依赖：
    - 真实 MySQL（Mock Repository）
    - 真实百炼 API（Mock EmbeddingClient）
    - 真实 .env / Settings（_FakeSettings）
"""

import hmac
import unittest
from unittest.mock import Mock, patch

from backend.core.exceptions import (
    ApiKeyNotConfiguredError,
    ApiKeyValidationError,
    PluginCredentialsMissingError,
    PluginDeleteConfirmationError,
    PluginDisabledError,
    PluginNameTakenError,
    PluginNameValidationError,
    PluginNotFoundError,
    PluginSecretMismatchError,
)
from backend.core.security import hash_plugin_secret
from backend.models.plugin import PluginStatus, PluginWorkspace
from backend.services.plugin_service import (
    PluginService,
    normalize_plugin_name,
    validate_plugin_name,
)


class _FakeSettings:
    """最小 Settings 替身：仅提供 PluginService 需要的 app_master_key（32 bytes）。"""

    app_master_key: str = "k" * 32


def _make_workspace(
    plugin_id: str = "plugin-id-1",
    plugin_name: str = "My Plugin",
    plugin_name_norm: str = "my plugin",
    plugin_secret_hash: str | None = None,
    status: str = PluginStatus.ACTIVE,
    api_key_ciphertext: str | None = None,
    api_key_nonce: str | None = None,
) -> PluginWorkspace:
    """构造 PluginWorkspace 内存对象（Mock 场景不落库）。默认 secret 为 "secret-1"。"""
    if plugin_secret_hash is None:
        plugin_secret_hash = hash_plugin_secret("secret-1")
    return PluginWorkspace(
        plugin_id=plugin_id,
        plugin_name=plugin_name,
        plugin_name_norm=plugin_name_norm,
        plugin_secret_hash=plugin_secret_hash,
        status=status,
        api_key_ciphertext=api_key_ciphertext,
        api_key_nonce=api_key_nonce,
    )


# ============================================================================
# plugin_name 规则
# ============================================================================


class TestPluginNameRules(unittest.TestCase):
    def test_validate_returns_trimmed_name(self):
        self.assertEqual(validate_plugin_name("  My Plugin  "), "My Plugin")
        self.assertEqual(validate_plugin_name("我的AI助手"), "我的AI助手")

    def test_validate_rejects_control_characters(self):
        for bad in ("a\nb", "a\tb", "a\rb", "a\x00b"):
            with self.subTest(name=bad):
                with self.assertRaises(PluginNameValidationError):
                    validate_plugin_name(bad)

    def test_validate_rejects_empty(self):
        for bad in ("", "   ", None):
            with self.subTest(name=bad):
                with self.assertRaises(PluginNameValidationError):
                    validate_plugin_name(bad)

    def test_validate_rejects_length_out_of_range(self):
        with self.assertRaises(PluginNameValidationError):
            validate_plugin_name("a")
        with self.assertRaises(PluginNameValidationError):
            validate_plugin_name("x" * 33)

    def test_validate_rejects_special_characters(self):
        for bad in ("ab@cd", "ab/cd", "ab,cd", "ab#cd", "ab，cd", "ab(cd)"):
            with self.subTest(name=bad):
                with self.assertRaises(PluginNameValidationError):
                    validate_plugin_name(bad)

    def test_normalize_compresses_whitespace_and_lower(self):
        self.assertEqual(normalize_plugin_name("  My AI Plugin  "), "my ai plugin")
        self.assertEqual(normalize_plugin_name("My   AI   Plugin"), "my ai plugin")
        self.assertEqual(normalize_plugin_name("我的 AI 助手"), "我的 ai 助手")
        self.assertEqual(normalize_plugin_name("我的AI助手"), "我的ai助手")


# ============================================================================
# Register
# ============================================================================


class TestPluginServiceRegister(unittest.TestCase):
    def _service(self, repo=None, embedding=None):
        return PluginService(repo or Mock(), _FakeSettings(), embedding)

    def test_register_success(self):
        repo = Mock()
        repo.get_by_plugin_name_norm.return_value = None
        repo.create_plugin.return_value = _make_workspace(plugin_id="generated-id")
        workspace, secret = self._service(repo).register("My Plugin")
        self.assertEqual(workspace.plugin_id, "generated-id")
        self.assertTrue(secret)
        _, kwargs = repo.create_plugin.call_args
        self.assertEqual(kwargs["plugin_name"], "My Plugin")
        self.assertEqual(kwargs["plugin_name_norm"], "my plugin")
        repo.get_by_plugin_name_norm.assert_called_once_with("my plugin")

    def test_register_plugin_id_auto_generated(self):
        repo = Mock()
        repo.get_by_plugin_name_norm.return_value = None
        repo.create_plugin.return_value = _make_workspace()
        self._service(repo).register("My Plugin")
        _, kwargs = repo.create_plugin.call_args
        self.assertTrue(kwargs["plugin_id"])
        self.assertGreaterEqual(len(kwargs["plugin_id"]), 40)  # 约 43 字符
        self.assertNotEqual(kwargs["plugin_id"], "My Plugin")  # 不使用名称

    def test_register_plugin_secret_auto_generated(self):
        repo = Mock()
        repo.get_by_plugin_name_norm.return_value = None
        repo.create_plugin.return_value = _make_workspace()
        _, secret = self._service(repo).register("My Plugin")
        self.assertTrue(secret)
        self.assertGreaterEqual(len(secret), 40)  # 约 43 字符

    def test_register_secret_not_in_repository_args(self):
        repo = Mock()
        repo.get_by_plugin_name_norm.return_value = None
        repo.create_plugin.return_value = _make_workspace()
        _, secret = self._service(repo).register("My Plugin")
        _, kwargs = repo.create_plugin.call_args
        self.assertNotIn("plugin_secret", kwargs)  # 无明文 secret 参数
        self.assertNotIn(secret, repr(kwargs))  # 任何位置都不出现明文

    def test_register_secret_hash_correct(self):
        repo = Mock()
        repo.get_by_plugin_name_norm.return_value = None
        repo.create_plugin.return_value = _make_workspace()
        _, secret = self._service(repo).register("My Plugin")
        _, kwargs = repo.create_plugin.call_args
        self.assertEqual(kwargs["plugin_secret_hash"], hash_plugin_secret(secret))

    def test_register_duplicate_name(self):
        repo = Mock()
        repo.get_by_plugin_name_norm.return_value = _make_workspace()
        with self.assertRaises(PluginNameTakenError):
            self._service(repo).register("My Plugin")
        repo.create_plugin.assert_not_called()

    def test_register_normalizes_name(self):
        repo = Mock()
        repo.get_by_plugin_name_norm.return_value = None
        repo.create_plugin.return_value = _make_workspace()
        self._service(repo).register("  My   AI Plugin  ")
        _, kwargs = repo.create_plugin.call_args
        self.assertEqual(kwargs["plugin_name_norm"], "my ai plugin")

    def test_register_invalid_name(self):
        for bad in ("", " ", "a", "x" * 33, "bad\nname", "bad@name"):
            with self.subTest(name=bad):
                with self.assertRaises(PluginNameValidationError):
                    self._service().register(bad)
                Mock().create_plugin.assert_not_called()


# ============================================================================
# Authenticate
# ============================================================================


class TestPluginServiceAuthenticate(unittest.TestCase):
    def _service(self, repo=None):
        return PluginService(repo or Mock(), _FakeSettings())

    def test_authenticate_success(self):
        repo = Mock()
        repo.get_by_plugin_id.return_value = _make_workspace(
            plugin_secret_hash=hash_plugin_secret("secret-1")
        )
        workspace = self._service(repo).authenticate("plugin-id-1", "secret-1")
        self.assertEqual(workspace.plugin_id, "plugin-id-1")
        repo.get_by_plugin_id.assert_called_once_with("plugin-id-1")

    def test_authenticate_unknown_plugin(self):
        repo = Mock()
        repo.get_by_plugin_id.return_value = None
        with self.assertRaises(PluginNotFoundError):
            self._service(repo).authenticate("nope", "secret-1")

    def test_authenticate_wrong_secret(self):
        repo = Mock()
        repo.get_by_plugin_id.return_value = _make_workspace(
            plugin_secret_hash=hash_plugin_secret("right-secret")
        )
        with self.assertRaises(PluginSecretMismatchError):
            self._service(repo).authenticate("plugin-id-1", "wrong-secret")

    def test_authenticate_disabled(self):
        repo = Mock()
        repo.get_by_plugin_id.return_value = _make_workspace(
            plugin_secret_hash=hash_plugin_secret("secret-1"),
            status=PluginStatus.DISABLED,
        )
        with self.assertRaises(PluginDisabledError):
            self._service(repo).authenticate("plugin-id-1", "secret-1")

    def test_authenticate_missing_plugin_id(self):
        with self.assertRaises(PluginCredentialsMissingError):
            self._service().authenticate("", "secret-1")

    def test_authenticate_missing_secret(self):
        with self.assertRaises(PluginCredentialsMissingError):
            self._service().authenticate("plugin-id-1", "")


# ============================================================================
# API Key
# ============================================================================


class TestPluginServiceApiKey(unittest.TestCase):
    def _service(self, repo=None, embedding=None):
        return PluginService(repo or Mock(), _FakeSettings(), embedding)

    def test_update_api_key_success(self):
        repo = Mock()
        repo.update_api_key.return_value = _make_workspace(
            api_key_ciphertext="ct", api_key_nonce="nonce"
        )
        embedding = Mock()
        embedding.embed.return_value = [[0.1]]
        svc = self._service(repo, embedding)
        with patch(
            "backend.services.plugin_service.encrypt_api_key",
            return_value=("ct-new", "nonce-new"),
        ):
            result = svc.update_api_key("plugin-id-1", "sk-new-key")
        self.assertEqual(result.api_key_ciphertext, "ct")
        # 最小验证使用「用户提交的 Key」（embedding，非 LLM）
        _, kwargs = embedding.embed.call_args
        self.assertEqual(kwargs.get("api_key"), "sk-new-key")
        repo.update_api_key.assert_called_once_with("plugin-id-1", "ct-new", "nonce-new")

    def test_update_api_key_decryptable(self):
        # 真实 AES-256-GCM 闭环：update 后 decrypt 可还原明文
        repo = Mock()
        repo.update_api_key.side_effect = (
            lambda pid, ct, n: _make_workspace(api_key_ciphertext=ct, api_key_nonce=n)
        )
        embedding = Mock()
        embedding.embed.return_value = [[0.1]]
        svc = self._service(repo, embedding)
        result = svc.update_api_key("plugin-id-1", "sk-decrypt-me")
        self.assertEqual(svc.decrypt_api_key(result), "sk-decrypt-me")

    def test_update_api_key_validation_failure(self):
        embedding = Mock()
        embedding.embed.side_effect = RuntimeError("bailian rejected")
        repo = Mock()
        svc = self._service(repo, embedding)
        with self.assertRaises(ApiKeyValidationError):
            svc.update_api_key("plugin-id-1", "sk-bad")
        repo.update_api_key.assert_not_called()

    def test_update_api_key_empty(self):
        with self.assertRaises(ApiKeyValidationError):
            self._service().update_api_key("plugin-id-1", "   ")

    def test_update_api_key_non_sk_prefix(self):
        with self.assertRaises(ApiKeyValidationError):
            self._service().update_api_key("plugin-id-1", "not-a-key")

    def test_decrypt_api_key_not_configured(self):
        workspace = _make_workspace()  # ciphertext / nonce 为 None
        with self.assertRaises(ApiKeyNotConfiguredError):
            self._service().decrypt_api_key(workspace)

    def test_decrypt_api_key_success(self):
        workspace = _make_workspace(api_key_ciphertext="ct", api_key_nonce="nonce")
        with patch(
            "backend.services.plugin_service._decrypt_ciphertext",
            return_value="sk-plain",
        ):
            plain = self._service().decrypt_api_key(workspace)
        self.assertEqual(plain, "sk-plain")

    def test_remove_api_key_clears(self):
        repo = Mock()
        repo.clear_api_key.return_value = _make_workspace(
            api_key_ciphertext=None, api_key_nonce=None
        )
        svc = self._service(repo)
        result = svc.remove_api_key("plugin-id-1")
        repo.clear_api_key.assert_called_once_with("plugin-id-1")
        self.assertIsNone(result.api_key_ciphertext)
        self.assertIsNone(result.api_key_nonce)

    def test_remove_api_key_keeps_plugin_id(self):
        repo = Mock()
        repo.clear_api_key.return_value = _make_workspace(
            plugin_id="plugin-id-1",
            plugin_secret_hash=hash_plugin_secret("secret-1"),
        )
        result = self._service(repo).remove_api_key("plugin-id-1")
        self.assertEqual(result.plugin_id, "plugin-id-1")

    def test_remove_api_key_keeps_secret_hash(self):
        repo = Mock()
        repo.clear_api_key.return_value = _make_workspace(
            plugin_id="plugin-id-1",
            plugin_secret_hash=hash_plugin_secret("secret-1"),
        )
        result = self._service(repo).remove_api_key("plugin-id-1")
        self.assertEqual(result.plugin_secret_hash, hash_plugin_secret("secret-1"))


# ============================================================================
# 修改名称
# ============================================================================


class TestPluginServiceName(unittest.TestCase):
    def _service(self, repo=None):
        return PluginService(repo or Mock(), _FakeSettings())

    def test_update_plugin_name_success(self):
        repo = Mock()
        repo.get_by_plugin_id.return_value = _make_workspace(
            plugin_name="Old Name", plugin_name_norm="old name"
        )
        repo.get_by_plugin_name_norm.return_value = None
        repo.update_plugin_name.return_value = _make_workspace(
            plugin_name="New Name", plugin_name_norm="new name"
        )
        result = self._service(repo).update_plugin_name("plugin-id-1", "New Name")
        self.assertEqual(result.plugin_name, "New Name")
        repo.update_plugin_name.assert_called_once_with(
            "plugin-id-1", "New Name", "new name"
        )

    def test_update_plugin_name_taken(self):
        repo = Mock()
        repo.get_by_plugin_id.return_value = _make_workspace(
            plugin_name="Old Name", plugin_name_norm="old name"
        )
        repo.get_by_plugin_name_norm.return_value = _make_workspace(
            plugin_id="other-id", plugin_name="New Name", plugin_name_norm="new name"
        )
        with self.assertRaises(PluginNameTakenError):
            self._service(repo).update_plugin_name("plugin-id-1", "New Name")
        repo.update_plugin_name.assert_not_called()

    def test_update_plugin_name_keeps_plugin_id_and_secret_and_api_key(self):
        repo = Mock()
        repo.get_by_plugin_id.return_value = _make_workspace()
        repo.get_by_plugin_name_norm.return_value = None
        repo.update_plugin_name.return_value = _make_workspace(
            plugin_id="plugin-id-1",
            plugin_name="New Name",
            plugin_name_norm="new name",
            plugin_secret_hash=hash_plugin_secret("secret-1"),
            api_key_ciphertext="ct",
            api_key_nonce="nonce",
        )
        result = self._service(repo).update_plugin_name("plugin-id-1", "New Name")
        self.assertEqual(result.plugin_id, "plugin-id-1")
        self.assertEqual(result.plugin_secret_hash, hash_plugin_secret("secret-1"))
        self.assertEqual(result.api_key_ciphertext, "ct")
        self.assertEqual(result.api_key_nonce, "nonce")

    def test_update_plugin_name_not_found(self):
        repo = Mock()
        repo.get_by_plugin_id.return_value = None
        with self.assertRaises(PluginNotFoundError):
            self._service(repo).update_plugin_name("nope", "New Name")


# ============================================================================
# 删除 Workspace
# ============================================================================


class TestPluginServiceDelete(unittest.TestCase):
    def _service(self, repo=None):
        return PluginService(repo or Mock(), _FakeSettings())

    def test_delete_workspace_confirm_false(self):
        repo = Mock()
        repo.get_by_plugin_id.return_value = _make_workspace(plugin_name="My Plugin")
        with self.assertRaises(PluginDeleteConfirmationError):
            self._service(repo).delete_workspace("plugin-id-1", False, "My Plugin")
        repo.delete_plugin.assert_not_called()

    def test_delete_workspace_name_mismatch(self):
        repo = Mock()
        repo.get_by_plugin_id.return_value = _make_workspace(plugin_name="My Plugin")
        with self.assertRaises(PluginDeleteConfirmationError):
            self._service(repo).delete_workspace("plugin-id-1", True, "Wrong Name")
        repo.delete_plugin.assert_not_called()

    def test_delete_workspace_success(self):
        repo = Mock()
        repo.get_by_plugin_id.return_value = _make_workspace(plugin_name="My Plugin")
        self._service(repo).delete_workspace("plugin-id-1", True, "My Plugin")
        repo.delete_plugin.assert_called_once_with("plugin-id-1")

    def test_delete_workspace_not_found(self):
        repo = Mock()
        repo.get_by_plugin_id.return_value = None
        with self.assertRaises(PluginNotFoundError):
            self._service(repo).delete_workspace("nope", True, "My Plugin")


# ============================================================================
# get_plugin
# ============================================================================


class TestPluginServiceGet(unittest.TestCase):
    def test_get_plugin_found(self):
        repo = Mock()
        repo.get_by_plugin_id.return_value = _make_workspace()
        workspace = PluginService(repo, _FakeSettings()).get_plugin("plugin-id-1")
        self.assertEqual(workspace.plugin_id, "plugin-id-1")

    def test_get_plugin_not_found(self):
        repo = Mock()
        repo.get_by_plugin_id.return_value = None
        with self.assertRaises(PluginNotFoundError):
            PluginService(repo, _FakeSettings()).get_plugin("nope")


# ============================================================================
# 安全
# ============================================================================


class TestPluginServiceSecurity(unittest.TestCase):
    def _service(self, repo=None, embedding=None):
        return PluginService(repo or Mock(), _FakeSettings(), embedding)

    def test_secret_not_logged(self):
        repo = Mock()
        repo.get_by_plugin_name_norm.return_value = None
        repo.create_plugin.return_value = _make_workspace()
        svc = self._service(repo)
        with patch("backend.services.plugin_service.logger") as mock_logger:
            _, secret = svc.register("My Plugin")
        for call in (
            mock_logger.info.call_args_list + mock_logger.warning.call_args_list
        ):
            self.assertNotIn(secret, " ".join(str(a) for a in call.args))

    def test_api_key_not_logged(self):
        embedding = Mock()
        embedding.embed.side_effect = RuntimeError("bad key sk-secret-456")
        svc = self._service(repo=Mock(), embedding=embedding)
        with patch("backend.services.plugin_service.logger") as mock_logger:
            with self.assertRaises(ApiKeyValidationError) as cm:
                svc.update_api_key("plugin-id-1", "sk-secret-456")
        self.assertNotIn("sk-secret-456", str(cm.exception))
        for call in mock_logger.warning.call_args_list:
            self.assertNotIn("sk-secret-456", " ".join(str(a) for a in call.args))

    def test_repository_receives_no_plain_secret(self):
        repo = Mock()
        repo.get_by_plugin_name_norm.return_value = None
        repo.create_plugin.return_value = _make_workspace()
        _, secret = self._service(repo).register("My Plugin")
        _, kwargs = repo.create_plugin.call_args
        self.assertNotIn("plugin_secret", kwargs)
        self.assertNotIn(secret, repr(kwargs))

    def test_secret_compared_with_compare_digest(self):
        repo = Mock()
        repo.get_by_plugin_id.return_value = _make_workspace(
            plugin_secret_hash=hash_plugin_secret("secret-1")
        )
        svc = self._service(repo)
        with patch(
            "backend.services.plugin_service.hmac.compare_digest",
            wraps=hmac.compare_digest,
        ) as cmp:
            svc.authenticate("plugin-id-1", "secret-1")
        cmp.assert_called()

    def test_authenticate_error_messages_no_leak(self):
        repo = Mock()
        repo.get_by_plugin_id.return_value = _make_workspace(
            plugin_secret_hash=hash_plugin_secret("right-secret")
        )
        with self.assertRaises(PluginSecretMismatchError) as cm:
            self._service(repo).authenticate("plugin-id-1", "wrong-secret")
        message = str(cm.exception)
        self.assertNotIn("plugin-id-1", message)
        self.assertNotIn("wrong-secret", message)


if __name__ == "__main__":
    unittest.main()
