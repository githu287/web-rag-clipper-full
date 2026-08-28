"""
Security 纯工具单元测试（Phase 3.5 Step 2-H 清理后：仅 Plugin + API Key 能力）。

测试策略：
- 纯函数测试：无 DB / 无 Settings / 无 IO；
- master key 由测试显式构造 32 bytes 字符串，不依赖 .env；
- 覆盖：sha256 / token 生成 / AES-256-GCM round trip / 随机 nonce /
  篡改与错误密钥失败路径 / 密钥长度校验 / Plugin Secret hash 与生成。

不依赖：
- 真实 MySQL / Redis / Milvus；
- FastAPI / Service / API。
"""

from __future__ import annotations

import base64
import hashlib
import unittest

from backend.core.exceptions import (
    SecurityConfigurationError,
    SecurityDecryptionError,
)
from backend.core.security import (
    decrypt_api_key,
    encrypt_api_key,
    generate_plugin_id,
    generate_plugin_secret,
    generate_token,
    hash_plugin_secret,
    sha256_hex,
)

# 32 bytes 的 ASCII 字符串，utf-8 编码后恰好 32 bytes（AES-256）
_MASTER_KEY: str = "k" * 32


class SecurityUtilTest(unittest.TestCase):
    """backend/core/security.py 纯函数测试。"""

    def test_sha256_hex_correct(self) -> None:
        """sha256_hex 结果与 hashlib 一致，且为 64 位十六进制。"""
        self.assertEqual(
            sha256_hex("hello"),
            hashlib.sha256(b"hello").hexdigest(),
        )
        self.assertEqual(len(sha256_hex("hello")), 64)

    def test_generate_token_non_empty(self) -> None:
        """generate_token 生成非空随机 token。"""
        token = generate_token()
        self.assertTrue(token)
        self.assertGreater(len(token), 20)

    def test_encrypt_decrypt_round_trip(self) -> None:
        """AES-256-GCM 加密后能正确解密还原。"""
        api_key = "sk-test-1234567890"
        ciphertext, nonce = encrypt_api_key(api_key, _MASTER_KEY)
        self.assertEqual(
            decrypt_api_key(ciphertext, nonce, _MASTER_KEY),
            api_key,
        )

    def test_encrypt_same_key_ciphertext_differs(self) -> None:
        """两次加密同一 API Key，因 nonce 随机，ciphertext 不应相同。"""
        api_key = "sk-test-abcdef"
        c1, n1 = encrypt_api_key(api_key, _MASTER_KEY)
        c2, n2 = encrypt_api_key(api_key, _MASTER_KEY)
        self.assertNotEqual(c1, c2)
        self.assertNotEqual(n1, n2)
        # 两次密文都能独立解密还原
        self.assertEqual(decrypt_api_key(c1, n1, _MASTER_KEY), api_key)
        self.assertEqual(decrypt_api_key(c2, n2, _MASTER_KEY), api_key)

    def test_ciphertext_not_plaintext(self) -> None:
        """密文（含解码后）不含明文 API Key，字符串级也不等于明文。"""
        api_key = "sk-test-abcdef"
        ciphertext, _ = encrypt_api_key(api_key, _MASTER_KEY)
        self.assertNotEqual(ciphertext, api_key)
        decoded = base64.b64decode(ciphertext)
        self.assertNotIn(api_key.encode("utf-8"), decoded)

    def test_nonce_non_empty(self) -> None:
        """nonce 非空且解码后为 12 bytes（GCM 推荐长度）。"""
        _, nonce = encrypt_api_key("sk-test", _MASTER_KEY)
        self.assertTrue(nonce)
        self.assertEqual(len(base64.b64decode(nonce)), 12)

    def test_wrong_master_key_decrypt_fails(self) -> None:
        """错误 master key 解密必须失败（GCM tag 验证失败）。"""
        ciphertext, nonce = encrypt_api_key("sk-test", _MASTER_KEY)
        wrong_key: str = "w" * 32
        with self.assertRaises(SecurityDecryptionError):
            decrypt_api_key(ciphertext, nonce, wrong_key)

    def test_tampered_ciphertext_fails(self) -> None:
        """篡改密文任意字节后解密必须失败（GCM tag 验证失败）。"""
        ciphertext, nonce = encrypt_api_key("sk-test", _MASTER_KEY)
        raw = bytearray(base64.b64decode(ciphertext))
        raw[0] ^= 0xFF  # 翻转密文首字节
        tampered = base64.b64encode(bytes(raw)).decode("ascii")
        with self.assertRaises(SecurityDecryptionError):
            decrypt_api_key(tampered, nonce, _MASTER_KEY)

    def test_master_key_not_32_bytes_fails(self) -> None:
        """master key 非 32 bytes 时 encrypt/decrypt 均抛配置错误。"""
        for bad_key in ("short", "k" * 31, "k" * 33):
            with self.assertRaises(SecurityConfigurationError):
                encrypt_api_key("sk-test", bad_key)
            with self.assertRaises(SecurityConfigurationError):
                decrypt_api_key("dummy", "dummy", bad_key)


class PluginSecretTest(unittest.TestCase):
    """Plugin Secret / Plugin ID 工具测试（Phase 3.5 Step 2-C 新增）。"""

    def test_generate_plugin_id_length(self) -> None:
        """plugin_id 为约 43 字符（token_urlsafe(32)，256-bit 随机性）。"""
        plugin_id = generate_plugin_id()
        self.assertTrue(plugin_id)
        self.assertGreaterEqual(len(plugin_id), 40)

    def test_generate_plugin_secret_length(self) -> None:
        """plugin_secret 为约 43 字符（token_urlsafe(32)，256-bit 随机性）。"""
        secret = generate_plugin_secret()
        self.assertTrue(secret)
        self.assertGreaterEqual(len(secret), 40)

    def test_generate_random_values_differ(self) -> None:
        """两次生成的值应不同（随机性）。"""
        self.assertNotEqual(generate_plugin_id(), generate_plugin_id())
        self.assertNotEqual(generate_plugin_secret(), generate_plugin_secret())

    def test_hash_plugin_secret_length(self) -> None:
        """hash_plugin_secret 输出 64 位十六进制（SHA-256）。"""
        self.assertEqual(len(hash_plugin_secret("secret-1")), 64)

    def test_hash_plugin_secret_stable(self) -> None:
        """相同 secret 的 hash 一致。"""
        secret = "my-plugin-secret-123"
        self.assertEqual(
            hash_plugin_secret(secret), hash_plugin_secret(secret)
        )

    def test_hash_plugin_secret_differs_for_different_secrets(self) -> None:
        """不同 secret 的 hash 不同。"""
        self.assertNotEqual(
            hash_plugin_secret("secret-a"), hash_plugin_secret("secret-b")
        )

    def test_hash_plugin_secret_not_plaintext(self) -> None:
        """hash 不含明文 secret（数据库只保存 hash）。"""
        secret = "plain-secret-xyz"
        self.assertNotEqual(secret, hash_plugin_secret(secret))
        self.assertNotIn(secret, hash_plugin_secret(secret))


if __name__ == "__main__":
    unittest.main()
