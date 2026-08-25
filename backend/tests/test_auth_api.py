"""
Auth API 集成测试（Phase 3.4 Step 4）。

技术栈：unittest + unittest.mock + FastAPI TestClient。
不连接真实 MySQL / Milvus / 百炼：
    - 通过 app.dependency_overrides 将 get_user_service 替换为 fake user service；
    - 通过 app.dependency_overrides[get_current_user] 固定返回 fake user
      （/auth/api-key 正常路径）；
    - 单独测试类不覆盖 get_current_user，验证「无 / 无效 Bearer token → 401」；
    - 通过 patch("backend.main.get_milvus_initializer") 阻断 lifespan 启动期
      真实 Milvus 连接。

覆盖场景：
    A. POST /auth/register → 201 + { user_id, token, token_type="Bearer" }
    B. POST /auth/register 已注册 → 409（ApiKeyAlreadyRegisteredError）
    C. POST /auth/login → 200 + token
    D. POST /auth/login 未注册 / 无效 Key → 401（ApiKeyInvalidError）
    E. PUT /auth/api-key（带 Bearer）→ 200 + { user_id }
    F. PUT /auth/api-key 无 / 无效 token → 401（AuthenticationError）
    G. body 缺 api_key / 空字符串 → 422（Pydantic min_length=1）
    H. extra request 字段 → 422（extra="forbid"）
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from backend.api.deps import get_current_user
from backend.core.di import get_user_repository, get_user_service
from backend.core.exceptions import (
    ApiKeyAlreadyRegisteredError,
    ApiKeyInvalidError,
)
from backend.main import create_app


class AuthApiTest(unittest.TestCase):
    """/auth 正常路径（fake user service + 固定 current_user）。"""

    def setUp(self) -> None:
        self._milvus_init_patcher = patch("backend.main.get_milvus_initializer")
        self.mock_initializer = Mock()
        self.mock_initializer.initialize.return_value = None
        self._milvus_init_patcher.start().return_value = self.mock_initializer

        self.fake_user = SimpleNamespace(id=1)
        self.fake_user_service = Mock()
        self.fake_user_service.register = Mock()
        self.fake_user_service.login = Mock()
        self.fake_user_service.update_api_key = Mock()

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

        response = self.client.post("/auth/register", json={"api_key": "sk-test"})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.json(),
            {"user_id": 1, "token": "token-abc", "token_type": "Bearer"},
        )
        self.fake_user_service.register.assert_called_once_with("sk-test")

    def test_register_already_registered_409(self) -> None:
        """B：已注册 → 409 Conflict。"""
        self.fake_user_service.register.side_effect = ApiKeyAlreadyRegisteredError(
            "api key already registered"
        )

        response = self.client.post("/auth/register", json={"api_key": "sk-test"})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["type"], "ApiKeyAlreadyRegisteredError")

    # ------------------------------------------------------------------ login
    def test_login_200(self) -> None:
        """C：登录成功 → 200 + token。"""
        self.fake_user_service.login.return_value = (self.fake_user, "token-xyz")

        response = self.client.post("/auth/login", json={"api_key": "sk-test"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["token"], "token-xyz")
        self.assertEqual(response.json()["user_id"], 1)
        self.fake_user_service.login.assert_called_once_with("sk-test")

    def test_login_invalid_key_401(self) -> None:
        """D：未注册 / 无效 Key → 401。"""
        self.fake_user_service.login.side_effect = ApiKeyInvalidError(
            "api key not registered"
        )

        response = self.client.post("/auth/login", json={"api_key": "sk-wrong"})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["type"], "ApiKeyInvalidError")

    # -------------------------------------------------------------- api-key
    def test_api_key_update_200(self) -> None:
        """E：换 Key 成功 → 200 + { user_id }；token 不变语义由 Service 保证。"""
        self.fake_user_service.update_api_key.return_value = self.fake_user

        response = self.client.put(
            "/auth/api-key",
            json={"api_key": "sk-new"},
            headers={"Authorization": "Bearer token-abc"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"user_id": 1})
        self.fake_user_service.update_api_key.assert_called_once_with(1, "sk-new")

    # ------------------------------------------------------------- validation
    def test_register_empty_api_key_422(self) -> None:
        """G：空字符串 / 缺字段 → 422（Pydantic min_length=1）。"""
        response = self.client.post("/auth/register", json={"api_key": ""})
        self.assertEqual(response.status_code, 422)
        response = self.client.post("/auth/register", json={})
        self.assertEqual(response.status_code, 422)

    def test_register_extra_field_422(self) -> None:
        """H：extra 字段 → 422（extra="forbid"）。"""
        response = self.client.post(
            "/auth/register", json={"api_key": "sk-test", "hacker": 1}
        )
        self.assertEqual(response.status_code, 422)


class AuthApiUnauthorizedTest(unittest.TestCase):
    """/auth/api-key 未认证路径（不覆盖 get_current_user，走真实依赖）。"""

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

    def test_api_key_update_without_token_401(self) -> None:
        """无 Authorization 头 → 401（AuthenticationError）。"""
        response = self.client.put("/auth/api-key", json={"api_key": "sk-new"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["type"], "AuthenticationError")

    def test_api_key_update_invalid_token_401(self) -> None:
        """无效 Bearer token → 401（repo 查不到 → AuthenticationError）。"""
        response = self.client.put(
            "/auth/api-key",
            json={"api_key": "sk-new"},
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["type"], "AuthenticationError")
        self.fake_user_repo.get_user_by_token_hash.assert_called_once()

    def test_api_key_update_malformed_header_401(self) -> None:
        """非 Bearer 格式 → 401。"""
        response = self.client.put(
            "/auth/api-key",
            json={"api_key": "sk-new"},
            headers={"Authorization": "Basic abc"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["type"], "AuthenticationError")


if __name__ == "__main__":
    unittest.main()
