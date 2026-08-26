"""
Security 纯工具单元测试（Phase 3.4 Step 3；F-REV3 新增 Argon2id 密码测试）。

测试策略：
- 纯函数测试：无 DB / 无 Settings / 无 IO；
- master key 由测试显式构造 32 bytes 字符串，不依赖 .env；
- 覆盖：sha256 / token 生成与哈希 / AES-256-GCM round trip / 随机 nonce /
  篡改与错误密钥失败路径 / 密钥长度校验 / Argon2id 密码哈希与强度校验。

不依赖：
- 真实 MySQL / Redis / Milvus；
- FastAPI / Service / API。
"""

from __future__ import annotations

import base64
import hashlib
import unittest

from backend.core.exceptions import (
    PasswordPolicyError,
    SecurityConfigurationError,
    SecurityDecryptionError,
)
from backend.core.security import (
    decrypt_api_key,
    encrypt_api_key,
    generate_token,
    hash_password,
    hash_token,
    sha256_hex,
    validate_password_strength,
    verify_password,
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

    def test_hash_token_stable(self) -> None:
        """同一 token 的 hash 稳定，且为 64 位十六进制。"""
        token = generate_token()
        self.assertEqual(hash_token(token), hash_token(token))
        self.assertEqual(len(hash_token(token)), 64)

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

    def test_token_plaintext_not_equal_hash(self) -> None:
        """token 明文与 token_hash 不相等（数据库只保存 hash）。"""
        token = generate_token()
        self.assertNotEqual(token, hash_token(token))

    def test_master_key_not_32_bytes_fails(self) -> None:
        """master key 非 32 bytes 时 encrypt/decrypt 均抛配置错误。"""
        for bad_key in ("short", "k" * 31, "k" * 33):
            with self.assertRaises(SecurityConfigurationError):
                encrypt_api_key("sk-test", bad_key)
            with self.assertRaises(SecurityConfigurationError):
                decrypt_api_key("dummy", "dummy", bad_key)


class PasswordHashTest(unittest.TestCase):
    """Argon2id 密码哈希与强度校验测试（Phase 3.4 Step F-REV3）。"""

    def test_hash_password_argon2id_phc(self) -> None:
        """hash_password 输出 $argon2id$ 前缀的 PHC 格式字符串。"""
        h = hash_password("correct-horse-12")
        self.assertTrue(h.startswith("$argon2id$"), f"unexpected prefix: {h[:24]}")
        self.assertGreater(len(h), 40)

    def test_hash_password_random_salt(self) -> None:
        """同一密码两次哈希结果不同（随机 salt），且均可验证通过。"""
        h1 = hash_password("same-password-123")
        h2 = hash_password("same-password-123")
        self.assertNotEqual(h1, h2)
        self.assertTrue(verify_password("same-password-123", h1))
        self.assertTrue(verify_password("same-password-123", h2))

    def test_verify_password_correct(self) -> None:
        """正确密码验证通过。"""
        h = hash_password("my-secret-pass")
        self.assertTrue(verify_password("my-secret-pass", h))

    def test_verify_password_wrong(self) -> None:
        """错误密码验证失败。"""
        h = hash_password("my-secret-pass")
        self.assertFalse(verify_password("wrong-pass", h))

    def test_verify_password_invalid_hash_returns_false(self) -> None:
        """非法 / 损坏哈希统一返回 False，不抛异常、不泄露细节。"""
        self.assertFalse(verify_password("any-password", "not-a-valid-hash"))
        self.assertFalse(verify_password("any-password", ""))
        self.assertFalse(verify_password("any-password", "$argon2id$truncated"))

    def test_hash_not_contain_plaintext(self) -> None:
        """哈希字符串不包含明文密码。"""
        pwd = "super-secret-pass-42"
        self.assertNotIn(pwd, hash_password(pwd))

    def test_validate_password_strength_ok(self) -> None:
        """最小 8 位 / 最大 128 位边界内通过。"""
        validate_password_strength("12345678")
        validate_password_strength("a" * 128)

    def test_validate_password_strength_too_short(self) -> None:
        """长度 < 8（含空串）抛 PasswordPolicyError。"""
        for bad in ("", "1234567"):
            with self.assertRaises(PasswordPolicyError):
                validate_password_strength(bad)

    def test_validate_password_strength_too_long(self) -> None:
        """长度 > 128 抛 PasswordPolicyError。"""
        with self.assertRaises(PasswordPolicyError):
            validate_password_strength("a" * 129)


if __name__ == "__main__":
    unittest.main()
