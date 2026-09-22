"""Workspace 多服务商模型配置回归测试。"""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from backend.core.exceptions import ApiKeyValidationError
from backend.models.model_provider import (
    WorkspaceModelCredentials,
    build_endpoint,
    credentials_from_payload,
    legacy_dashscope_credentials,
    provider_catalog,
)
from backend.services.plugin_service import PluginService


class _Settings:
    app_master_key = "m" * 32


def _credentials() -> WorkspaceModelCredentials:
    return WorkspaceModelCredentials(
        build_endpoint(
            kind="embedding",
            provider="openai",
            api_key="openai-embedding-key",
            model="text-embedding-3-small",
        ),
        build_endpoint(
            kind="llm",
            provider="deepseek",
            api_key="deepseek-chat-key",
            model="deepseek-chat",
        ),
    )


class ModelProviderTest(unittest.TestCase):
    def test_independent_embedding_and_llm_credentials(self) -> None:
        credentials = _credentials()
        self.assertIsInstance(credentials, str)
        self.assertEqual(str(credentials), "openai-embedding-key")
        self.assertEqual(credentials.embedding.provider, "openai")
        self.assertEqual(credentials.llm.provider, "deepseek")

    def test_payload_round_trip(self) -> None:
        original = _credentials()
        restored = credentials_from_payload(original.to_encrypted_payload())
        self.assertEqual(restored.embedding, original.embedding)
        self.assertEqual(restored.llm, original.llm)

    def test_known_provider_locks_official_base_url(self) -> None:
        endpoint = build_endpoint(
            kind="llm",
            provider="deepseek",
            api_key="key",
            model="deepseek-chat",
            base_url="https://attacker.example/v1",
        )
        self.assertEqual(endpoint.base_url, "https://api.deepseek.com/v1")

    def test_custom_provider_requires_safe_https_url(self) -> None:
        with self.assertRaises(ValueError):
            build_endpoint(
                kind="llm", provider="custom", api_key="key", model="model",
                base_url="http://127.0.0.1:9999/v1",
            )
        endpoint = build_endpoint(
            kind="llm", provider="custom", api_key="key", model="model",
            base_url="https://models.example.com/v1/",
        )
        self.assertEqual(endpoint.base_url, "https://models.example.com/v1")

    def test_catalog_covers_chat_only_providers(self) -> None:
        catalog = provider_catalog()
        llm_ids = {item["id"] for item in catalog["llm"]}
        expected = {
            "dashscope", "openai", "gemini", "deepseek", "openrouter", "custom"
        }
        self.assertTrue(expected <= llm_ids)

    def test_legacy_key_maps_to_dashscope(self) -> None:
        credentials = legacy_dashscope_credentials("sk-legacy")
        self.assertEqual(credentials.embedding.provider, "dashscope")
        self.assertEqual(credentials.llm.provider, "dashscope")

    def test_service_encrypts_validates_and_decrypts_config(self) -> None:
        repo = Mock()
        embedding = Mock()
        llm = Mock()
        embedding.embed.return_value = [[0.0] * 1024]
        llm.generate.return_value = "OK"
        repo.get_by_plugin_id.return_value = SimpleNamespace(
            embedding_config_fingerprint=None
        )
        repo.update_api_key.side_effect = (
            lambda plugin_id, ciphertext, nonce, fingerprint: SimpleNamespace(
                plugin_id=plugin_id,
                api_key_ciphertext=ciphertext,
                api_key_nonce=nonce,
                embedding_config_fingerprint=fingerprint,
            )
        )
        service = PluginService(repo, _Settings(), embedding, llm)
        credentials = _credentials()
        workspace = service.update_model_config("plugin-1", credentials)
        restored = service.decrypt_api_key(workspace)
        self.assertIsInstance(restored, WorkspaceModelCredentials)
        self.assertEqual(restored.embedding.api_key, "openai-embedding-key")
        self.assertEqual(restored.llm.api_key, "deepseek-chat-key")
        embedding.embed.assert_called_once()
        llm.generate.assert_called_once()
        ciphertext = repo.update_api_key.call_args.args[1]
        self.assertNotIn("openai-embedding-key", ciphertext)
        self.assertNotIn("deepseek-chat-key", ciphertext)

    def test_rejects_embedding_space_change_when_documents_exist(self) -> None:
        repo = Mock()
        documents = Mock()
        repo.get_by_plugin_id.return_value = SimpleNamespace(
            embedding_config_fingerprint="existing-space"
        )
        documents.count_documents.return_value = 1
        service = PluginService(
            repo, _Settings(), Mock(), Mock(), document_repository=documents
        )

        with self.assertRaisesRegex(ApiKeyValidationError, "先删除已有文档"):
            service.update_model_config("plugin-1", _credentials())

        repo.update_api_key.assert_not_called()


if __name__ == "__main__":
    unittest.main()
