"""
Auth API 集成测试（Phase 3.4 Step 4；F-REV3 username+password 身份模型）。

技术栈：unittest + unittest.mock + FastAPI TestClient。
不连接真实 MySQL / Milvus / 百炼：
    - 通过 app.dependency_overrides 将 get_user_service 替换为 fake user service；
    - 正常路径类覆盖 get_current_user 固定返回 fake user；
    - 未认证路径类不覆盖 get_current_user，走真实 deps.get_current_user，
      验证「无 / 无效 Bearer token → 401」；
    - 通过 patch("backend.main.get_milvus_initializer") 阻断 lifespan 启动期
      真实 Milvus 连接。

覆盖场景：
    A. POST /auth/register → 201 + { user_id, token, token_type="Bearer" }
    B. POST /auth/register username 已存在 → 409（UsernameAlreadyExistsError）
    C. POST /auth/register 密码强度不足 → 422（PasswordPolicyError）
    D. POST /auth/login → 200 + token（轮换，旧 token 失效由 Service 保证）
    E. POST /auth/login username 不存在 / 密码错误 → 401（InvalidCredentialsError）
    F. POST /auth/login 账号被禁用 → 403（DisabledUserError）
    G. POST /auth/logout（Bearer）→ 204
    H. POST /auth/logout 无 / 无效 token → 401（AuthenticationError）
    I. GET /users/me（Bearer）→ 200 + { user_id, username, api_key_configured, created_at }
    J. PUT /users/me/api-key（Bearer）→ 200 + { user_id }
    K. DELETE /users/me/api-key（Bearer）→ 204
    L. PUT /users/me/api-key 无 / 无效 token → 401（AuthenticationError）
    M. 旧 PUT /auth/api-key 已删除（F-REV3 迁移到 /users/me/api-key）→ 404
    N. body 校验：缺字段 / 空值 / 非法 username / 非 sk- 前缀 Key / extra 字段 → 422
"""

from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from backend.api.deps import get_current_user
from backend.core.di import get_user_repository, get_user_service
from backend.core.exceptions import (
    DisabledUserError,
    InvalidCredentialsError,
    PasswordPolicyError,
    UsernameAlreadyExistsError,
)
from backend.main import create_app


class AuthApiTest(unittest.TestCase):
    """/auth 与 /users/me 正常路径（fake user service + 固定 current_user）。"""

    def setUp(self) -> None:
        self._milvus_init_patcher = patch("backend.main.get_milvus_initializer")
        self.mock_initializer = Mock()
        self.mock_initializer.initialize.return_value = None
        self._milvus_init_patcher.start().return_value = self.mock_initializer

        self.fake_user = SimpleNamespace(
            id=1,
            username="alice",
            api_key_ciphertext=None,
            api_key_nonce=None,
            created_at=datetime(2026, 1, 1, 0, 0, 0),
        )
        self.fake_user_service = Mock()
        self.fake_user_service.register = Mock()
        self.fake_user_service.login = Mock()
        self.fake_user_service.logout = Mock()
        self.fake_user_service.update_api_key = Mock()
        self.fake_user_service.remove_api_key = Mock()
        self.fake_user_service.get_current_user = Mock()

        self.app = create_app()
        self.app.dependency_overrides[get_user_service] = (
            lambda: self.fake_user_service
        )
        self.app.dependency_overrides[get_current_user] = (
            lambda: self.fake_user
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self._milvus_init_patcher.stop()
        self.app.dependency_overrides.clear()

    # -------------------------------------------------------------- register
    def test_register_201(self) -> None:
        """A：注册成功 → 201 + user_id / token / token_type=Bearer。"""
        self.fake_user_service.register.return_value = (self.fake_user, "token-abc")

        response = self.client.post(
            "/auth/register",
            json={"username": "alice", "password": "correct-horse-battery"},
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.json(),
            {"user_id": 1, "token": "token-abc", "token_type": "Bearer"},
        )
        self.fake_user_service.register.assert_called_once_with(
            "alice", "correct-horse-battery"
        )

    def test_register_username_taken_409(self) -> None:
        """B：username 已存在 → 409 Conflict。"""
        self.fake_user_service.register.side_effect = UsernameAlreadyExistsError(
            "username already exists: 'alice'"
        )

        response = self.client.post(
            "/auth/register",
            json={"username": "alice", "password": "correct-horse-battery"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["type"], "UsernameAlreadyExistsError")

    def test_register_weak_password_422(self) -> None:
        """C：密码强度不足 → 422（PasswordPolicyError）。"""
        self.fake_user_service.register.side_effect = PasswordPolicyError(
            "password must be 8-128 chars"
        )

        response = self.client.post(
            "/auth/register",
            json={"username": "alice", "password": "short"},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["type"], "PasswordPolicyError")

    # ------------------------------------------------------------------ login
    def test_login_200(self) -> None:
        """D：登录成功 → 200 + token。"""
        self.fake_user_service.login.return_value = (self.fake_user, "token-xyz")

        response = self.client.post(
            "/auth/login",
            json={"username": "alice", "password": "correct-horse-battery"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["token"], "token-xyz")
        self.assertEqual(response.json()["user_id"], 1)
        self.fake_user_service.login.assert_called_once_with(
            "alice", "correct-horse-battery"
        )

    def test_login_wrong_credentials_401(self) -> None:
        """E：username 不存在 / 密码错误 → 401（统一语义，防枚举）。"""
        self.fake_user_service.login.side_effect = InvalidCredentialsError(
            "invalid username or password"
        )

        response = self.client.post(
            "/auth/login",
            json={"username": "alice", "password": "wrong-password"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["type"], "InvalidCredentialsError")

    def test_login_disabled_403(self) -> None:
        """F：账号被禁用 → 403 Forbidden。"""
        self.fake_user_service.login.side_effect = DisabledUserError(
            "user is disabled"
        )

        response = self.client.post(
            "/auth/login",
            json={"username": "alice", "password": "correct-horse-battery"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["type"], "DisabledUserError")

    # ----------------------------------------------------------------- logout
    def test_logout_204(self) -> None:
        """G：登出成功 → 204；token 立即失效由 Service 保证。"""
        response = self.client.post(
            "/auth/logout",
            headers={"Authorization": "Bearer token-abc"},
        )

        self.assertEqual(response.status_code, 204)
        self.fake_user_service.logout.assert_called_once_with(1)

    # ---------------------------------------------------------------- users/me
    def test_get_me_200(self) -> None:
        """I：当前用户信息 → 200；不含任何凭据字段。"""
        self.fake_user_service.get_current_user.return_value = self.fake_user

        response = self.client.get(
            "/users/me",
            headers={"Authorization": "Bearer token-abc"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "user_id": 1,
                "username": "alice",
                "api_key_configured": False,
                "created_at": "2026-01-01T00:00:00",
            },
        )
        self.fake_user_service.get_current_user.assert_called_once_with(1)

    def test_get_me_api_key_configured_true(self) -> None:
        """I'：ciphertext 与 nonce 均非 NULL → api_key_configured=True。"""
        self.fake_user.api_key_ciphertext = "ciphertext-b64"
        self.fake_user.api_key_nonce = "nonce-b64"
        self.fake_user_service.get_current_user.return_value = self.fake_user

        response = self.client.get(
            "/users/me",
            headers={"Authorization": "Bearer token-abc"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["api_key_configured"])
        self.assertNotIn("api_key", response.json())

    # ------------------------------------------------------- /users/me/api-key
    def test_update_api_key_200(self) -> None:
        """J：配置 / 更换 API Key → 200 + { user_id }；token 不变语义由 Service 保证。"""
        self.fake_user_service.update_api_key.return_value = self.fake_user

        response = self.client.put(
            "/users/me/api-key",
            json={"api_key": "sk-new-key"},
            headers={"Authorization": "Bearer token-abc"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"user_id": 1})
        self.fake_user_service.update_api_key.assert_called_once_with(
            1, "sk-new-key"
        )

    def test_remove_api_key_204(self) -> None:
        """K：清除 API Key → 204。"""
        response = self.client.delete(
            "/users/me/api-key",
            headers={"Authorization": "Bearer token-abc"},
        )

        self.assertEqual(response.status_code, 204)
        self.fake_user_service.remove_api_key.assert_called_once_with(1)

    # ------------------------------------------------------------ legacy route
    def test_legacy_auth_api_key_route_404(self) -> None:
        """M：旧 PUT /auth/api-key 已删除（F-REV3 迁移到 /users/me/api-key）→ 404。"""
        response = self.client.put(
            "/auth/api-key",
            json={"api_key": "sk-new"},
            headers={"Authorization": "Bearer token-abc"},
        )

        self.assertEqual(response.status_code, 404)

    # ------------------------------------------------------------- validation
    def test_register_validation_422(self) -> None:
        """N：register body 校验 → 422（Pydantic 约束 + extra="forbid"）。"""
        cases = [
            {"username": "", "password": "correct-horse-battery"},  # 空 username
            {"username": "alice"},  # 缺 password
            {"username": "alice", "password": ""},  # 空 password
            {"username": "al ice", "password": "correct-horse-battery"},  # 非法字符
            {"username": "alice", "password": "x" * 129},  # 超长 password
            {
                "username": "alice",
                "password": "correct-horse-battery",
                "hacker": 1,
            },  # extra 字段
        ]
        for body in cases:
            with self.subTest(body=body):
                response = self.client.post("/auth/register", json=body)
                self.assertEqual(response.status_code, 422)

    def test_login_validation_422(self) -> None:
        """N'：login body 校验 → 422。"""
        cases = [
            {"username": "alice"},  # 缺 password
            {"username": "", "password": "correct-horse-battery"},  # 空 username
            {"username": "ali ce", "password": "correct-horse-battery"},  # 非法字符
        ]
        for body in cases:
            with self.subTest(body=body):
                response = self.client.post("/auth/login", json=body)
                self.assertEqual(response.status_code, 422)

    def test_api_key_update_validation_422(self) -> None:
        """N''：api-key body 校验 → 422（必须 sk- 前缀 + extra="forbid"）。"""
        cases = [
            {"api_key": "not-a-sk-key"},  # 非 sk- 前缀
            {"api_key": ""},  # 空字符串
            {"api_key": "sk-ok", "hacker": 1},  # extra 字段
        ]
        for body in cases:
            with self.subTest(body=body):
                response = self.client.put(
                    "/users/me/api-key",
                    json=body,
                    headers={"Authorization": "Bearer token-abc"},
                )
                self.assertEqual(response.status_code, 422)


class AuthApiUnauthorizedTest(unittest.TestCase):
    """/auth/logout 与 /users/me* 未认证路径（不覆盖 get_current_user，走真实依赖）。"""

    def setUp(self) -> None:
        self._milvus_init_patcher = patch("backend.main.get_milvus_initializer")
        self.mock_initializer = Mock()
        self.mock_initializer.initialize.return_value = None
        self._milvus_init_patcher.start().return_value = self.mock_initializer

        self.fake_user_service = Mock()
        self.fake_user_repo = Mock()
        self.fake_user_repo.get_user_by_token_hash = Mock(return_value=None)

        self.app = create_app()
        self.app.dependency_overrides[get_user_service] = (
            lambda: self.fake_user_service
        )
        self.app.dependency_overrides[get_user_repository] = (
            lambda: self.fake_user_repo
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self._milvus_init_patcher.stop()
        self.app.dependency_overrides.clear()

    def test_logout_without_token_401(self) -> None:
        """H：无 Authorization 头 → 401（AuthenticationError）。"""
        response = self.client.post("/auth/logout")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["type"], "AuthenticationError")

    def test_get_me_invalid_token_401(self) -> None:
        """无效 Bearer token → 401（repo 查不到 → AuthenticationError）。"""
        response = self.client.get(
            "/users/me",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["type"], "AuthenticationError")
        self.fake_user_repo.get_user_by_token_hash.assert_called_once()

    def test_update_api_key_without_token_401(self) -> None:
        """L：无 Authorization 头 → 401。"""
        response = self.client.put(
            "/users/me/api-key",
            json={"api_key": "sk-new-key"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["type"], "AuthenticationError")

    def test_update_api_key_invalid_token_401(self) -> None:
        """L'：无效 Bearer token → 401。"""
        response = self.client.put(
            "/users/me/api-key",
            json={"api_key": "sk-new-key"},
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["type"], "AuthenticationError")

    def test_remove_api_key_malformed_header_401(self) -> None:
        """非 Bearer 格式 → 401（不触发 repo 查询）。"""
        response = self.client.delete(
            "/users/me/api-key",
            headers={"Authorization": "Basic abc"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["type"], "AuthenticationError")
        self.fake_user_repo.get_user_by_token_hash.assert_not_called()


if __name__ == "__main__":
    unittest.main()
