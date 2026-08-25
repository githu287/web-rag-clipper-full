"""
UserService 单元测试（Phase 3.4 Step 4）。

测试策略：
1) Mock UserRepository（unittest.mock.Mock），聚焦 UserService 编排逻辑：
   - register：新 Key → create_user + 返回 token；已存在 → ApiKeyAlreadyRegisteredError；
   - login：已注册 → update_token 轮换；未注册 → ApiKeyInvalidError；
     被禁用 → ApiKeyInvalidError；
   - update_api_key：委托 repo 并透传加密副本；
   - decrypt_api_key：真实 AES-256-GCM round trip（32 bytes master key）；
   - UserOperationError → AuthOperationError 包装（503 语义）。
2) master key 显式构造 "k"*32（32 bytes ASCII），不依赖 .env。

不依赖：
- 真实 MySQL / Milvus / 百炼；
- FastAPI / API / Settings 单例。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from backend.core.exceptions import (
    ApiKeyAlreadyRegisteredError,
    ApiKeyInvalidError,
    AuthOperationError,
    UserOperationError,
)
from backend.core.security import encrypt_api_key, hash_token, sha256_hex
from backend.models.user import UserStatus
from backend.services.user_service import UserService

_MASTER_KEY: str = "k" * 32


def _make_service(repo: Mock | None = None) -> tuple[UserService, Mock]:
    """构造 UserService（master key 为 32 bytes）+ 注入的 Mock repo。"""
    repo = repo or Mock()
    settings = SimpleNamespace(app_master_key=_MASTER_KEY)
    return UserService(user_repository=repo, settings=settings), repo


def _fake_user(
    user_id: int = 1,
    status: str = UserStatus.ACTIVE,
    ciphertext: str = "ciphertext-x",
    nonce: str = "nonce-x",
) -> SimpleNamespace:
    """构造 User ORM 的行为替身（仅含本 Service 使用的字段）。"""
    return SimpleNamespace(
        id=user_id,
        status=status,
        api_key_hash=sha256_hex("sk-test"),
        api_key_ciphertext=ciphertext,
        api_key_nonce=nonce,
    )


class UserServiceRegisterTest(unittest.TestCase):
    """register() 编排行为。"""

    def test_register_new_user_creates_and_returns_token(self) -> None:
        """新 API Key：create_user 被调用，返回 (User, 非空 token)。"""
        service, repo = _make_service()
        repo.get_user_by_api_key_hash.return_value = None
        repo.create_user.return_value = _fake_user()

        user, token = service.register("sk-test-1")

        self.assertEqual(user.id, 1)
        self.assertTrue(token)
        repo.create_user.assert_called_once()
        # token_hash 必须是 sha256(token)（DB 不存明文）
        self.assertEqual(
            repo.create_user.call_args.kwargs["token_hash"],
            hash_token(token),
        )
        # create_user 入参包含 sha256(api_key) 与加密副本
        self.assertEqual(
            repo.create_user.call_args.kwargs["api_key_hash"],
            sha256_hex("sk-test-1"),
        )

    def test_register_already_registered_raises_conflict(self) -> None:
        """已注册：抛 ApiKeyAlreadyRegisteredError（应改走 login）。"""
        service, repo = _make_service()
        repo.get_user_by_api_key_hash.return_value = _fake_user()

        with self.assertRaises(ApiKeyAlreadyRegisteredError):
            service.register("sk-test-1")
        repo.create_user.assert_not_called()

    def test_register_empty_api_key_raises(self) -> None:
        """空 / 全空白 Key：ApiKeyInvalidError，且不触碰 DB。"""
        service, repo = _make_service()
        for bad in ("", "   ", "\t\n"):
            with self.assertRaises(ApiKeyInvalidError):
                service.register(bad)
        repo.get_user_by_api_key_hash.assert_not_called()

    def test_register_repo_failure_wraps_auth_operation_error(self) -> None:
        """create_user 抛 UserOperationError → AuthOperationError（503 语义）。"""
        service, repo = _make_service()
        repo.get_user_by_api_key_hash.return_value = None
        repo.create_user.side_effect = UserOperationError("db down")

        with self.assertRaises(AuthOperationError):
            service.register("sk-test-1")


class UserServiceLoginTest(unittest.TestCase):
    """login() 编排行为。"""

    def test_login_success_rotates_token(self) -> None:
        """已注册 + ACTIVE：update_token 被调用（token 轮换），返回新 token。"""
        service, repo = _make_service()
        repo.get_user_by_api_key_hash.return_value = _fake_user()

        user, token = service.login("sk-test-1")

        self.assertEqual(user.id, 1)
        self.assertTrue(token)
        repo.update_token.assert_called_once_with(1, hash_token(token))

    def test_login_not_registered_raises(self) -> None:
        """未注册：ApiKeyInvalidError（401），update_token 不执行。"""
        service, repo = _make_service()
        repo.get_user_by_api_key_hash.return_value = None

        with self.assertRaises(ApiKeyInvalidError):
            service.login("sk-test-1")
        repo.update_token.assert_not_called()

    def test_login_disabled_user_raises(self) -> None:
        """status=DISABLED：ApiKeyInvalidError，不允许登录。"""
        service, repo = _make_service()
        repo.get_user_by_api_key_hash.return_value = _fake_user(status=UserStatus.DISABLED)

        with self.assertRaises(ApiKeyInvalidError):
            service.login("sk-test-1")
        repo.update_token.assert_not_called()

    def test_login_empty_api_key_raises(self) -> None:
        """空 Key：ApiKeyInvalidError。"""
        service, repo = _make_service()
        with self.assertRaises(ApiKeyInvalidError):
            service.login("   ")

    def test_login_repo_failure_wraps_auth_operation_error(self) -> None:
        """update_token 抛 UserOperationError → AuthOperationError。"""
        service, repo = _make_service()
        repo.get_user_by_api_key_hash.return_value = _fake_user()
        repo.update_token.side_effect = UserOperationError("db down")

        with self.assertRaises(AuthOperationError):
            service.login("sk-test-1")


class UserServiceUpdateApiKeyTest(unittest.TestCase):
    """update_api_key() 编排行为。"""

    def test_update_api_key_delegates_to_repo(self) -> None:
        """换 Key：repo.update_api_key 收到加密副本 + sha256 hash。"""
        service, repo = _make_service()
        repo.update_api_key.return_value = _fake_user()

        result = service.update_api_key(user_id=7, api_key="sk-new")

        self.assertEqual(result.id, 1)
        kwargs = repo.update_api_key.call_args.kwargs
        self.assertEqual(kwargs["user_id"], 7)
        self.assertEqual(kwargs["api_key_hash"], sha256_hex("sk-new"))
        # 明文不落库：ciphertext 不得等于明文
        self.assertNotEqual(kwargs["api_key_ciphertext"], "sk-new")

    def test_update_api_key_empty_raises(self) -> None:
        """空 Key：ApiKeyInvalidError。"""
        service, repo = _make_service()
        with self.assertRaises(ApiKeyInvalidError):
            service.update_api_key(user_id=7, api_key="")

    def test_update_api_key_repo_failure_wraps(self) -> None:
        """repo 抛 UserOperationError → AuthOperationError。"""
        service, repo = _make_service()
        repo.update_api_key.side_effect = UserOperationError("db down")
        with self.assertRaises(AuthOperationError):
            service.update_api_key(user_id=7, api_key="sk-new")


class UserServiceDecryptTest(unittest.TestCase):
    """decrypt_api_key() 行为。"""

    def test_decrypt_api_key_round_trip(self) -> None:
        """真实 AES round trip：解密结果等于原始明文。"""
        service, _ = _make_service()
        ciphertext, nonce = encrypt_api_key("sk-roundtrip", _MASTER_KEY)
        user = _fake_user(ciphertext=ciphertext, nonce=nonce)

        self.assertEqual(service.decrypt_api_key(user), "sk-roundtrip")

    def test_decrypt_api_key_wrong_master_key_fails(self) -> None:
        """错误 master key 解密必须失败（由 security 层抛 SecurityDecryptionError）。"""
        service, _ = _make_service()
        from backend.core.exceptions import SecurityDecryptionError

        ciphertext, nonce = encrypt_api_key("sk-x", "w" * 32)
        user = _fake_user(ciphertext=ciphertext, nonce=nonce)
        with self.assertRaises(SecurityDecryptionError):
            service.decrypt_api_key(user)


if __name__ == "__main__":
    unittest.main()
