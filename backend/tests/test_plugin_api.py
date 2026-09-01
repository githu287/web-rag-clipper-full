"""
Plugin API 集成测试（Phase 3.5 Step 2-D 新增）。

技术栈：unittest + unittest.mock + FastAPI TestClient。

不连接真实 MySQL / Milvus / 百炼：
    - 通过 app.dependency_overrides 将 get_plugin_service 替换为 fake
      PluginService（Mock）；register 的「secret 不进 DB」用例注入真实
      PluginService（Mock Repository）验证；
    - 正常路径类覆盖 get_current_plugin 固定返回 fake plugin；
    - 认证路径类不覆盖 get_current_plugin，走真实 deps.get_current_plugin，
      验证「缺失 / 无效 Plugin 头 → 401 / 403」；
    - 通过 patch("backend.main.get_milvus_initializer") 阻断 lifespan
      启动期真实 Milvus 连接。

覆盖场景（对应 Step 2-D §19 测试要求）：
    Register：201 / 响应字段 / secret 不进 DB / 重名 409 / 非法名 422 / extra 422
    Header 认证：缺 ID 401 / 缺 Secret 401 / 都缺 401 / id 不存在 401 /
                 secret 错 401 / disabled 403
    GET /plugins/me：正常 / 不含 secret / 不含 hash / 不含 ciphertext /
                     api_key_configured
    Update Name：正常 / 重名 409 / plugin_id 不变 / 响应不含 secret
    API Key：配置 200 / 响应不含 Key / 非法格式 422 / 删除 204 /
             删除后 configured=false / ApiKeyNotConfiguredError → 409
    Delete：confirm=false 400 / name 不匹配 400 / 正确确认 204
"""

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from backend.api.deps import get_current_plugin
from backend.core.di import get_plugin_service, get_workspace_delete_service
from backend.core.exceptions import (
    ApiKeyNotConfiguredError,
    PluginDeleteConfirmationError,
    PluginDisabledError,
    PluginNameTakenError,
    PluginNameValidationError,
    PluginNotFoundError,
    PluginSecretMismatchError,
)
from backend.core.security import hash_plugin_secret
from backend.main import create_app
from backend.models.plugin import PluginStatus, PluginWorkspace
from backend.services.plugin_service import PluginService


class _FakeSettings:
    """最小 Settings 替身：app_master_key 为 32 bytes（真实 AES 加密可用）。"""

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


class PluginApiTest(unittest.TestCase):
    """
    正常路径：override get_current_plugin 固定返回 fake plugin，
    验证各端点业务语义与响应字段安全红线。
    """

    def setUp(self) -> None:
        self._milvus_init_patcher = patch("backend.main.get_milvus_initializer")
        self.mock_initializer = Mock()
        self.mock_initializer.initialize.return_value = None
        self._milvus_init_patcher.start().return_value = self.mock_initializer

        self.fake_plugin = SimpleNamespace(
            plugin_id="plugin-id-1",
            plugin_name="My Plugin",
            status=PluginStatus.ACTIVE,
            api_key_ciphertext=None,
            api_key_nonce=None,
            created_at=datetime(2026, 1, 1, 0, 0, 0),
            updated_at=datetime(2026, 1, 1, 0, 0, 0),
        )
        self.fake_plugin_service = Mock()
        self.fake_workspace_delete_service = Mock()
        self.fake_workspace_delete_service.delete_workspace = AsyncMock()
        self.app = create_app()
        self.app.dependency_overrides[get_plugin_service] = (
            lambda: self.fake_plugin_service
        )
        self.app.dependency_overrides[get_workspace_delete_service] = (
            lambda: self.fake_workspace_delete_service
        )
        self.app.dependency_overrides[get_current_plugin] = lambda: self.fake_plugin
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self._milvus_init_patcher.stop()

    def _headers(self) -> dict[str, str]:
        return {"X-Plugin-ID": "plugin-id-1", "X-Plugin-Secret": "secret-1"}

    def _delete_json(self, url: str, body: dict) -> object:
        """DELETE 带 JSON body（TestClient.delete 便捷方法不支持 body，改用 request）。"""
        return self.client.request(
            "DELETE",
            url,
            json=body,
            headers=self._headers(),
        )

    # ------------------------------------------------------------ Register
    def test_register_201(self) -> None:
        self.fake_plugin_service.register.return_value = (
            self.fake_plugin,
            "secret-abc",
        )
        response = self.client.post(
            "/plugins/register", json={"plugin_name": "My Plugin"}
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.json(),
            {
                "plugin_id": "plugin-id-1",
                "plugin_name": "My Plugin",
                "plugin_secret": "secret-abc",
            },
        )
        self.fake_plugin_service.register.assert_called_once_with("My Plugin")

    def test_register_secret_not_in_db(self) -> None:
        """register 走真实 PluginService（Mock Repository）：secret 不进 DB 参数。"""
        repo = Mock()
        repo.get_by_plugin_name_norm.return_value = None
        repo.create_plugin.return_value = _make_workspace(
            plugin_id="plugin-id-1", plugin_name="My Plugin"
        )
        real_service = PluginService(repo, _FakeSettings())
        self.app.dependency_overrides[get_plugin_service] = lambda: real_service
        response = self.client.post(
            "/plugins/register", json={"plugin_name": "My Plugin"}
        )
        self.assertEqual(response.status_code, 201)
        secret = response.json()["plugin_secret"]
        _, kwargs = repo.create_plugin.call_args
        self.assertNotIn("plugin_secret", kwargs)
        self.assertNotIn(secret, repr(kwargs))

    def test_register_duplicate_name_409(self) -> None:
        self.fake_plugin_service.register.side_effect = PluginNameTakenError(
            "plugin name already taken: 'my plugin'"
        )
        response = self.client.post(
            "/plugins/register", json={"plugin_name": "My Plugin"}
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["type"], "PluginNameTakenError")

    def test_register_invalid_name_422(self) -> None:
        self.fake_plugin_service.register.side_effect = PluginNameValidationError(
            "plugin name must be 2-32 characters"
        )
        response = self.client.post(
            "/plugins/register", json={"plugin_name": "a"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["type"], "PluginNameValidationError")

    def test_register_extra_field_422(self) -> None:
        response = self.client.post(
            "/plugins/register",
            json={"plugin_name": "My Plugin", "hacker": 1},
        )
        self.assertEqual(response.status_code, 422)
        self.fake_plugin_service.register.assert_not_called()

    # ------------------------------------------------------------ GET /me
    def test_get_me_200(self) -> None:
        self.fake_plugin_service.get_plugin.return_value = self.fake_plugin
        response = self.client.get("/plugins/me", headers=self._headers())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["plugin_id"], "plugin-id-1")
        self.assertEqual(body["plugin_name"], "My Plugin")
        self.assertEqual(body["status"], PluginStatus.ACTIVE)
        self.assertFalse(body["api_key_configured"])
        self.assertEqual(body["created_at"], "2026-01-01T00:00:00")
        self.assertEqual(body["updated_at"], "2026-01-01T00:00:00")
        self.fake_plugin_service.get_plugin.assert_called_once_with("plugin-id-1")

    def test_get_me_no_secret_fields(self) -> None:
        self.fake_plugin_service.get_plugin.return_value = self.fake_plugin
        body = self.client.get("/plugins/me", headers=self._headers()).json()
        for field in (
            "plugin_secret",
            "plugin_secret_hash",
            "api_key_ciphertext",
            "api_key_nonce",
            "api_key",
        ):
            self.assertNotIn(field, body)

    def test_get_me_api_key_configured_true(self) -> None:
        self.fake_plugin.api_key_ciphertext = "ct"
        self.fake_plugin.api_key_nonce = "nonce"
        self.fake_plugin_service.get_plugin.return_value = self.fake_plugin
        body = self.client.get("/plugins/me", headers=self._headers()).json()
        self.assertTrue(body["api_key_configured"])

    # ------------------------------------------------------------ Update Name
    def test_update_name_200(self) -> None:
        renamed = SimpleNamespace(plugin_id="plugin-id-1", plugin_name="New Name")
        self.fake_plugin_service.update_plugin_name.return_value = renamed
        response = self.client.put(
            "/plugins/me",
            json={"plugin_name": "New Name"},
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"plugin_id": "plugin-id-1", "plugin_name": "New Name"},
        )
        self.fake_plugin_service.update_plugin_name.assert_called_once_with(
            "plugin-id-1", "New Name"
        )

    def test_update_name_taken_409(self) -> None:
        self.fake_plugin_service.update_plugin_name.side_effect = (
            PluginNameTakenError("plugin name already taken: 'new name'")
        )
        response = self.client.put(
            "/plugins/me",
            json={"plugin_name": "New Name"},
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["type"], "PluginNameTakenError")

    def test_update_name_keeps_plugin_id(self) -> None:
        renamed = SimpleNamespace(plugin_id="plugin-id-1", plugin_name="New Name")
        self.fake_plugin_service.update_plugin_name.return_value = renamed
        body = self.client.put(
            "/plugins/me",
            json={"plugin_name": "New Name"},
            headers=self._headers(),
        ).json()
        self.assertEqual(body["plugin_id"], "plugin-id-1")

    def test_update_name_response_no_secret(self) -> None:
        renamed = SimpleNamespace(plugin_id="plugin-id-1", plugin_name="New Name")
        self.fake_plugin_service.update_plugin_name.return_value = renamed
        body = self.client.put(
            "/plugins/me",
            json={"plugin_name": "New Name"},
            headers=self._headers(),
        ).json()
        for field in ("plugin_secret", "plugin_secret_hash"):
            self.assertNotIn(field, body)

    # ------------------------------------------------------------ API Key
    def test_update_api_key_200(self) -> None:
        updated = SimpleNamespace(
            plugin_id="plugin-id-1", api_key_ciphertext="ct", api_key_nonce="nonce"
        )
        self.fake_plugin_service.update_api_key.return_value = updated
        response = self.client.put(
            "/plugins/me/api-key",
            json={"api_key": "sk-new-key"},
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"plugin_id": "plugin-id-1", "api_key_configured": True},
        )
        self.fake_plugin_service.update_api_key.assert_called_once_with(
            "plugin-id-1", "sk-new-key"
        )

    def test_update_api_key_response_no_key(self) -> None:
        updated = SimpleNamespace(
            plugin_id="plugin-id-1", api_key_ciphertext="ct", api_key_nonce="nonce"
        )
        self.fake_plugin_service.update_api_key.return_value = updated
        body = self.client.put(
            "/plugins/me/api-key",
            json={"api_key": "sk-new-key"},
            headers=self._headers(),
        ).json()
        for field in ("api_key", "api_key_ciphertext", "api_key_nonce", "plugin_secret"):
            self.assertNotIn(field, body)

    def test_update_api_key_invalid_format_422(self) -> None:
        """非 sk- 前缀：Pydantic pattern 拦截 → 422，不进入 Service。"""
        response = self.client.put(
            "/plugins/me/api-key",
            json={"api_key": "not-a-key"},
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 422)
        self.fake_plugin_service.update_api_key.assert_not_called()

    def test_remove_api_key_204(self) -> None:
        response = self.client.delete(
            "/plugins/me/api-key", headers=self._headers()
        )
        self.assertEqual(response.status_code, 204)
        self.fake_plugin_service.remove_api_key.assert_called_once_with(
            "plugin-id-1"
        )

    def test_remove_api_key_then_get_me_configured_false(self) -> None:
        self.fake_plugin_service.get_plugin.return_value = self.fake_plugin
        response = self.client.delete(
            "/plugins/me/api-key", headers=self._headers()
        )
        self.assertEqual(response.status_code, 204)
        me = self.client.get("/plugins/me", headers=self._headers())
        self.assertFalse(me.json()["api_key_configured"])

    def test_api_key_not_configured_409(self) -> None:
        """ApiKeyNotConfiguredError → 409 handler 对 Plugin 场景复用生效。"""
        self.fake_plugin_service.get_plugin.side_effect = (
            ApiKeyNotConfiguredError(
                "当前 Workspace 尚未配置阿里云百炼 API Key，请前往设置配置。"
            )
        )
        response = self.client.get("/plugins/me", headers=self._headers())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["type"], "ApiKeyNotConfiguredError")

    # ------------------------------------------------------------ Delete
    def test_delete_confirm_false_400(self) -> None:
        self.fake_workspace_delete_service.delete_workspace.side_effect = (
            PluginDeleteConfirmationError(
                "workspace deletion requires confirm=True"
            )
        )
        response = self._delete_json(
            "/plugins/me",
            {"confirm": False, "plugin_name": "My Plugin"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["type"], "PluginDeleteConfirmationError"
        )

    def test_delete_name_mismatch_400(self) -> None:
        self.fake_workspace_delete_service.delete_workspace.side_effect = (
            PluginDeleteConfirmationError("plugin name does not match")
        )
        response = self._delete_json(
            "/plugins/me",
            {"confirm": True, "plugin_name": "Wrong"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["type"], "PluginDeleteConfirmationError"
        )

    def test_delete_success_204(self) -> None:
        response = self._delete_json(
            "/plugins/me",
            {"confirm": True, "plugin_name": "My Plugin"},
        )
        self.assertEqual(response.status_code, 204)
        self.fake_workspace_delete_service.delete_workspace.assert_awaited_once_with(
            plugin_id="plugin-id-1",
            confirm=True,
            plugin_name="My Plugin",
        )


class PluginApiAuthTest(unittest.TestCase):
    """
    认证路径：不 override get_current_plugin，走真实 deps.get_current_plugin
    （PluginService 注入 fake），验证 Plugin Header 缺失 / 无效的 HTTP 语义。
    """

    def setUp(self) -> None:
        self._milvus_init_patcher = patch("backend.main.get_milvus_initializer")
        self.mock_initializer = Mock()
        self.mock_initializer.initialize.return_value = None
        self._milvus_init_patcher.start().return_value = self.mock_initializer

        self.fake_plugin_service = Mock()
        self.app = create_app()
        self.app.dependency_overrides[get_plugin_service] = (
            lambda: self.fake_plugin_service
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self._milvus_init_patcher.stop()

    def _headers(self) -> dict[str, str]:
        return {"X-Plugin-ID": "plugin-id-1", "X-Plugin-Secret": "secret-1"}

    def test_missing_plugin_id_401(self) -> None:
        response = self.client.get(
            "/plugins/me", headers={"X-Plugin-Secret": "secret-1"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["type"], "PluginCredentialsMissingError"
        )
        self.fake_plugin_service.authenticate.assert_not_called()

    def test_missing_plugin_secret_401(self) -> None:
        response = self.client.get(
            "/plugins/me", headers={"X-Plugin-ID": "plugin-id-1"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["type"], "PluginCredentialsMissingError"
        )
        self.fake_plugin_service.authenticate.assert_not_called()

    def test_missing_both_headers_401(self) -> None:
        response = self.client.get("/plugins/me")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["type"], "PluginCredentialsMissingError"
        )
        self.fake_plugin_service.authenticate.assert_not_called()

    def test_plugin_not_found_401(self) -> None:
        self.fake_plugin_service.authenticate.side_effect = PluginNotFoundError(
            "plugin not found"
        )
        response = self.client.get(
            "/plugins/me",
            headers={"X-Plugin-ID": "nope", "X-Plugin-Secret": "secret-1"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["type"], "PluginNotFoundError")
        # 错误 detail 不泄露 plugin_id
        self.assertNotIn("nope", response.json()["detail"])

    def test_wrong_secret_401(self) -> None:
        self.fake_plugin_service.authenticate.side_effect = (
            PluginSecretMismatchError("plugin secret mismatch")
        )
        response = self.client.get(
            "/plugins/me",
            headers={"X-Plugin-ID": "plugin-id-1", "X-Plugin-Secret": "wrong"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["type"], "PluginSecretMismatchError")

    def test_disabled_403(self) -> None:
        self.fake_plugin_service.authenticate.side_effect = PluginDisabledError(
            "plugin is disabled"
        )
        response = self.client.get(
            "/plugins/me", headers=self._headers()
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["type"], "PluginDisabledError")


if __name__ == "__main__":
    unittest.main()
