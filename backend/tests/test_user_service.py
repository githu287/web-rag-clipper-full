"""
UserService 专项测试（Phase 3.4 Step 4；F-REV3 身份重构）。

覆盖范围（对应 F4 测试要求）：
    Register：成功 / 不需要 API Key / username 重复 409 / password <8 与 >128 422 /
              password_hash 不等于明文 / token 可立即认证；
    Login：正确凭据 / 密码错误 401 / username 不存在 401 / 两者语义一致 /
           disabled 403 / 不读取 API Key / 不调百炼 / 轮换 token（旧 token 失效）；
    Logout：clear_token / 旧 token 失效 / 异常包装；
    API Key：update_api_key 成功且不改 user_id/username/password_hash/token /
             验证失败 400 / remove_api_key 清空 / 清除后仍可登录 /
             decrypt_api_key 未配置 409 / 配置可解密；
    当前用户：get_current_user 正常 / 不存在；
    安全：异常消息不含 password / token / API Key。

测试策略：
    - 注入 Mock UserRepository（不连真实 MySQL）；
    - 注入 Mock EmbeddingClient（不调真实百炼）；
    - 密码哈希 / token 用真实 core.security；加密 / 验证用 patch 隔离外部副作用。
"""

import unittest
from unittest.mock import Mock, patch

from backend.core.exceptions import (
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
from backend.core.security import hash_token
from backend.models.user import User, UserStatus
from backend.services.user_service import UserService


class _FakeSettings:
    """最小 Settings 替身：仅提供 UserService 需要的 app_master_key。"""

    app_master_key: str = "test-master-key"


def _make_user(
    user_id: int = 1,
    username: str = "alice",
    password_hash: str = "hashed-pw",
    token_hash: str | None = "tok-hash",
    status: str = UserStatus.ACTIVE,
    api_key_ciphertext: str | None = None,
    api_key_nonce: str | None = None,
) -> User:
    return User(
        id=user_id,
        username=username,
        password_hash=password_hash,
        token_hash=token_hash,
        status=status,
        api_key_ciphertext=api_key_ciphertext,
        api_key_nonce=api_key_nonce,
    )


class TestUserServiceRegister(unittest.TestCase):
    def _service(self, repo=None):
        return UserService(repo or Mock(), _FakeSettings(), None)

    def test_register_success(self):
        repo = Mock()
        repo.get_user_by_username.return_value = None
        repo.create_user.return_value = _make_user()
        user, token = self._service(repo).register("alice", "StrongPass1")
        self.assertEqual(user.id, 1)
        self.assertTrue(token)
        _, kwargs = repo.create_user.call_args
        self.assertEqual(kwargs["username"], "alice")
        self.assertNotEqual(kwargs["password_hash"], "StrongPass1")
        self.assertTrue(kwargs["password_hash"].startswith("$argon2id$"))
        self.assertEqual(kwargs["token_hash"], hash_token(token))
        self.assertIsNone(kwargs["api_key_ciphertext"])
        self.assertIsNone(kwargs["api_key_nonce"])

    def test_register_does_not_require_api_key(self):
        # create_user 的 api_key 参数恒为 None —— 注册不依赖 API Key
        repo = Mock()
        repo.get_user_by_username.return_value = None
        repo.create_user.return_value = _make_user()
        self._service(repo).register("bob", "StrongPass1")
        _, kwargs = repo.create_user.call_args
        self.assertIsNone(kwargs.get("api_key_ciphertext"))
        self.assertIsNone(kwargs.get("api_key_nonce"))

    def test_register_username_exists(self):
        repo = Mock()
        repo.get_user_by_username.return_value = _make_user()
        with self.assertRaises(UsernameAlreadyExistsError):
            self._service(repo).register("alice", "StrongPass1")
        repo.create_user.assert_not_called()

    def test_register_password_too_short(self):
        with self.assertRaises(PasswordPolicyError):
            self._service().register("alice", "Short1")

    def test_register_password_too_long(self):
        with self.assertRaises(PasswordPolicyError):
            self._service().register("alice", "A" * 129)

    def test_register_password_hash_not_plaintext(self):
        repo = Mock()
        repo.get_user_by_username.return_value = None
        repo.create_user.return_value = _make_user()
        self._service(repo).register("alice", "StrongPass1")
        _, kwargs = repo.create_user.call_args
        self.assertNotIn("StrongPass1", kwargs["password_hash"])

    def test_register_token_immediately_usable(self):
        # token 返回的同时，DB 已写入 hash_token(token)，可直接用于 deps 认证
        repo = Mock()
        repo.get_user_by_username.return_value = None
        repo.create_user.return_value = _make_user()
        _, token = self._service(repo).register("alice", "StrongPass1")
        _, kwargs = repo.create_user.call_args
        self.assertEqual(kwargs["token_hash"], hash_token(token))

    def test_register_repo_failure_wrapped(self):
        repo = Mock()
        repo.get_user_by_username.return_value = None
        repo.create_user.side_effect = UserOperationError("db down")
        with self.assertRaises(AuthOperationError):
            self._service(repo).register("alice", "StrongPass1")


class TestUserServiceLogin(unittest.TestCase):
    def test_login_success(self):
        repo = Mock()
        repo.get_user_by_username.return_value = _make_user()
        svc = UserService(repo, _FakeSettings(), Mock())
        with (
            patch(
                "backend.services.user_service.verify_password",
                return_value=True,
            ),
            patch(
                "backend.services.user_service.generate_token",
                return_value="plain-token",
            ),
        ):
            user, token = svc.login("alice", "StrongPass1")
        self.assertEqual(user.id, 1)
        self.assertEqual(token, "plain-token")
        repo.update_token.assert_called_once_with(1, hash_token("plain-token"))

    def test_login_wrong_password(self):
        repo = Mock()
        repo.get_user_by_username.return_value = _make_user()
        svc = UserService(repo, _FakeSettings())
        with patch(
            "backend.services.user_service.verify_password",
            return_value=False,
        ):
            with self.assertRaises(InvalidCredentialsError):
                svc.login("alice", "wrong-pass")
        repo.update_token.assert_not_called()

    def test_login_unknown_username(self):
        repo = Mock()
        repo.get_user_by_username.return_value = None
        with self.assertRaises(InvalidCredentialsError):
            UserService(repo, _FakeSettings()).login("nobody", "whatever1")

    def test_login_error_semantics_identical(self):
        # 不存在 username 与密码错误必须同为 InvalidCredentialsError（防枚举）
        repo_missing = Mock()
        repo_missing.get_user_by_username.return_value = None
        repo_wrong = Mock()
        repo_wrong.get_user_by_username.return_value = _make_user()
        with patch(
            "backend.services.user_service.verify_password",
            return_value=False,
        ):
            with self.assertRaises(InvalidCredentialsError) as cm1:
                UserService(repo_missing, _FakeSettings()).login(
                    "ghost", "StrongPass1"
                )
            with self.assertRaises(InvalidCredentialsError) as cm2:
                UserService(repo_wrong, _FakeSettings()).login(
                    "alice", "wrong-pass"
                )
        self.assertEqual(type(cm1.exception), type(cm2.exception))
        self.assertEqual(str(cm1.exception), str(cm2.exception))

    def test_login_disabled_user(self):
        repo = Mock()
        repo.get_user_by_username.return_value = _make_user(
            status=UserStatus.DISABLED
        )
        svc = UserService(repo, _FakeSettings())
        with patch(
            "backend.services.user_service.verify_password",
            return_value=True,
        ):
            with self.assertRaises(DisabledUserError):
                svc.login("alice", "StrongPass1")
        repo.update_token.assert_not_called()

    def test_login_does_not_touch_api_key(self):
        # 登录只应调用 get_user_by_username / update_token，绝不访问 API Key
        repo = Mock()
        repo.get_user_by_username.return_value = _make_user()
        svc = UserService(repo, _FakeSettings())
        with (
            patch(
                "backend.services.user_service.verify_password",
                return_value=True,
            ),
            patch(
                "backend.services.user_service.generate_token",
                return_value="t",
            ),
        ):
            svc.login("alice", "StrongPass1")
        self.assertEqual(
            {c[0] for c in repo.method_calls},
            {"get_user_by_username", "update_token"},
        )

    def test_login_does_not_call_bailian(self):
        embedding = Mock()
        repo = Mock()
        repo.get_user_by_username.return_value = _make_user()
        svc = UserService(repo, _FakeSettings(), embedding)
        with (
            patch(
                "backend.services.user_service.verify_password",
                return_value=True,
            ),
            patch(
                "backend.services.user_service.generate_token",
                return_value="t",
            ),
        ):
            svc.login("alice", "StrongPass1")
        embedding.embed.assert_not_called()

    def test_login_rotates_token(self):
        # 每次登录 update_token 写入新 hash → 旧 token hash 被覆盖（立即失效）
        repo = Mock()
        repo.get_user_by_username.return_value = _make_user()
        svc = UserService(repo, _FakeSettings())
        with (
            patch(
                "backend.services.user_service.verify_password",
                return_value=True,
            ),
            patch(
                "backend.services.user_service.generate_token",
                return_value="new-token",
            ),
        ):
            svc.login("alice", "StrongPass1")
        repo.update_token.assert_called_once_with(1, hash_token("new-token"))


class TestUserServiceLogout(unittest.TestCase):
    def test_logout_clears_token(self):
        repo = Mock()
        UserService(repo, _FakeSettings()).logout(1)
        repo.clear_token.assert_called_once_with(1)

    def test_logout_unknown_user(self):
        repo = Mock()
        repo.clear_token.side_effect = UserNotFoundError(
            "user not found: id=999"
        )
        with self.assertRaises(UserNotFoundError):
            UserService(repo, _FakeSettings()).logout(999)

    def test_logout_db_failure_wrapped(self):
        repo = Mock()
        repo.clear_token.side_effect = UserOperationError("db down")
        with self.assertRaises(AuthOperationError):
            UserService(repo, _FakeSettings()).logout(1)


class TestUserServiceApiKey(unittest.TestCase):
    def test_update_api_key_success(self):
        repo = Mock()
        user = _make_user(api_key_ciphertext="ct", api_key_nonce="nonce")
        repo.update_api_key.return_value = user
        embedding = Mock()
        embedding.embed.return_value = [[0.1]]
        svc = UserService(repo, _FakeSettings(), embedding)
        with patch(
            "backend.services.user_service.encrypt_api_key",
            return_value=("ct-new", "nonce-new"),
        ):
            result = svc.update_api_key(1, "sk-new-key")
        self.assertEqual(result.api_key_ciphertext, "ct")
        # 最小验证使用「用户提交的 Key」（embedding，非 LLM）
        _, kwargs = embedding.embed.call_args
        self.assertEqual(kwargs.get("api_key"), "sk-new-key")
        repo.update_api_key.assert_called_once_with(1, "ct-new", "nonce-new")

    def test_update_api_key_keeps_identity_fields(self):
        repo = Mock()
        before = _make_user(
            user_id=7,
            username="alice",
            password_hash="ph",
            token_hash="th",
        )
        repo.update_api_key.return_value = before
        svc = UserService(repo, _FakeSettings(), Mock())
        with patch(
            "backend.services.user_service.encrypt_api_key",
            return_value=("c", "n"),
        ):
            after = svc.update_api_key(7, "sk-new-key")
        self.assertEqual(after.id, 7)
        self.assertEqual(after.username, "alice")
        self.assertEqual(after.password_hash, "ph")
        self.assertEqual(after.token_hash, "th")

    def test_update_api_key_validation_failure(self):
        embedding = Mock()
        embedding.embed.side_effect = RuntimeError("bailian rejected")
        repo = Mock()
        svc = UserService(repo, _FakeSettings(), embedding)
        with self.assertRaises(ApiKeyValidationError):
            svc.update_api_key(1, "sk-bad")
        repo.update_api_key.assert_not_called()

    def test_update_api_key_empty(self):
        with self.assertRaises(ApiKeyValidationError):
            UserService(Mock(), _FakeSettings(), None).update_api_key(1, "   ")

    def test_remove_api_key_clears(self):
        repo = Mock()
        repo.clear_api_key.return_value = _make_user(
            api_key_ciphertext=None, api_key_nonce=None
        )
        svc = UserService(repo, _FakeSettings())
        result = svc.remove_api_key(1)
        repo.clear_api_key.assert_called_once_with(1)
        self.assertIsNone(result.api_key_ciphertext)
        self.assertIsNone(result.api_key_nonce)

    def test_remove_api_key_keeps_login_capability(self):
        # 清除 API Key 后账号身份仍在（不删用户 / token / password / username）
        repo = Mock()
        repo.clear_api_key.return_value = _make_user(token_hash="tok-hash")
        after = UserService(repo, _FakeSettings()).remove_api_key(1)
        self.assertEqual(after.username, "alice")
        self.assertEqual(after.password_hash, "hashed-pw")
        self.assertEqual(after.token_hash, "tok-hash")

    def test_decrypt_api_key_not_configured(self):
        user = _make_user()  # ciphertext / nonce 为 None
        with self.assertRaises(ApiKeyNotConfiguredError):
            UserService(Mock(), _FakeSettings()).decrypt_api_key(user)

    def test_decrypt_api_key_success(self):
        user = _make_user(api_key_ciphertext="ct", api_key_nonce="nonce")
        with patch(
            "backend.services.user_service._decrypt_ciphertext",
            return_value="sk-plain",
        ):
            plain = UserService(Mock(), _FakeSettings()).decrypt_api_key(user)
        self.assertEqual(plain, "sk-plain")


class TestUserServiceCurrentUser(unittest.TestCase):
    def test_get_current_user_found(self):
        repo = Mock()
        repo.get_user_by_id.return_value = _make_user()
        user = UserService(repo, _FakeSettings()).get_current_user(1)
        self.assertEqual(user.id, 1)

    def test_get_current_user_not_found(self):
        repo = Mock()
        repo.get_user_by_id.return_value = None
        with self.assertRaises(UserNotFoundError):
            UserService(repo, _FakeSettings()).get_current_user(999)


class TestUserServiceSecurity(unittest.TestCase):
    def test_error_message_does_not_leak_password(self):
        repo = Mock()
        # F-REV3：register 先查重 username，需返回 None 才会走到 create_user
        repo.get_user_by_username.return_value = None
        repo.create_user.side_effect = UserOperationError("db down")
        svc = UserService(repo, _FakeSettings())
        with self.assertRaises(AuthOperationError) as cm:
            svc.register("alice", "SuperSecretPw1")
        self.assertNotIn("SuperSecretPw1", str(cm.exception))

    def test_error_message_does_not_leak_api_key(self):
        embedding = Mock()
        embedding.embed.side_effect = RuntimeError("bad key sk-secret-123")
        svc = UserService(Mock(), _FakeSettings(), embedding)
        with self.assertRaises(ApiKeyValidationError) as cm:
            svc.update_api_key(1, "sk-secret-123")
        self.assertNotIn("sk-secret-123", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
