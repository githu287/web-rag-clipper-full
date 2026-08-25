"""
纯安全工具模块（Phase 3.4 Step 3 用户体系）。

职责：
    提供与 API Key / Token 相关的纯安全计算，不感知任何业务：
    - sha256_hex(value)：SHA-256 十六进制哈希（api_key_hash / token_hash 的基础）
    - generate_token()：随机 opaque token（secrets.token_urlsafe(32)）
    - hash_token(token)：token → token_hash（SHA-256）
    - encrypt_api_key(api_key, master_key)：AES-256-GCM 加密，返回 (ciphertext_b64, nonce_b64)
    - decrypt_api_key(ciphertext, nonce, master_key)：AES-256-GCM 解密（验证 GCM tag）

范围边界：
    - 不读取任何配置（master key 由调用方显式传入，禁止在本模块 import Settings）；
    - 不感知数据库 / Service / API；
    - 不打印 / 不记录任何入参（api_key / token / ciphertext 一律不进日志，
      错误信息也不包含完整 api_key / token）。

安全红线（禁止）：
    - ECB / CBC 自行拼 MAC；
    - 固定 nonce（每次 encrypt 必须 secrets.token_bytes(12) 随机生成）；
    - APP_MASTER_KEY 非 32 bytes（AES-256 密钥长度）→ SecurityConfigurationError；
    - decrypt 跳过 GCM authentication tag 验证（AESGCM.decrypt 自带验证，
      InvalidTag → SecurityDecryptionError）。

存储契约（与 backend/models/user.py 对齐）：
    encrypt_api_key 返回 (ciphertext_b64, nonce_b64)：
    - ciphertext_b64 = base64(AES-256-GCM(nonce, plaintext))，含 GCM tag（密文+tag 一起编码）
    - nonce_b64      = base64(12 字节随机 nonce)
    两值直接存入 users.api_key_ciphertext / users.api_key_nonce。
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .exceptions import (
    SecurityConfigurationError,
    SecurityDecryptionError,
)

# AES-256-GCM 密钥长度（bytes）：master key 必须恰好 32 bytes
_MASTER_KEY_BYTES: Final[int] = 32

# GCM 推荐 nonce 长度（NIST 建议 12 bytes；cryptography AESGCM 要求 8-128 位）
_AES_GCM_NONCE_BYTES: Final[int] = 12


def _require_master_key_bytes(master_key: str) -> bytes:
    """校验 master key 为 32 bytes（utf-8 编码后），返回 bytes；否则抛配置错误。"""
    key = master_key.encode("utf-8")
    if len(key) != _MASTER_KEY_BYTES:
        raise SecurityConfigurationError(
            f"APP_MASTER_KEY must be exactly {_MASTER_KEY_BYTES} bytes "
            f"(utf-8 encoded), got {len(key)} bytes"
        )
    return key


def sha256_hex(value: str) -> str:
    """计算 value 的 SHA-256 十六进制（64 字符），用于 api_key_hash / token_hash。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def generate_token() -> str:
    """生成随机 opaque token（secrets.token_urlsafe(32)，约 43 字符）。

    明文 token 只返回给调用方一次，数据库只保存 hash_token(token)。
    """
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """token → token_hash（SHA-256 十六进制）。数据库只保存本值，不保存明文。"""
    return sha256_hex(token)


def encrypt_api_key(api_key: str, master_key: str) -> tuple[str, str]:
    """AES-256-GCM 加密 API Key。

    Args:
        api_key  : 真实 API Key 明文（仅存在于调用栈内存，不落库 / 不进日志）。
        master_key: 32 bytes 的 APP_MASTER_KEY（utf-8 编码后必须恰好 32 bytes）。

    Returns:
        (ciphertext_b64, nonce_b64)：
        - ciphertext_b64：GCM 密文 + 16 字节 authentication tag 一起 Base64；
        - nonce_b64      ：本次随机生成的 12 字节 nonce Base64（每条记录独立）。

    Raises:
        SecurityConfigurationError: master_key 非 32 bytes。
    """
    key = _require_master_key_bytes(master_key)
    nonce = secrets.token_bytes(_AES_GCM_NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, api_key.encode("utf-8"), None)
    return (
        base64.b64encode(ciphertext).decode("ascii"),
        base64.b64encode(nonce).decode("ascii"),
    )


def decrypt_api_key(ciphertext: str, nonce: str, master_key: str) -> str:
    """AES-256-GCM 解密 API Key，严格验证 GCM authentication tag。

    Args:
        ciphertext: encrypt_api_key 返回的 ciphertext_b64。
        nonce     : encrypt_api_key 返回的 nonce_b64。
        master_key: 32 bytes 的 APP_MASTER_KEY。

    Returns:
        解密后的 API Key 明文（仅存在于调用栈内存）。

    Raises:
        SecurityConfigurationError: master_key 非 32 bytes。
        SecurityDecryptionError   : Base64 损坏 / nonce 非法 / GCM tag 验证失败
                                    （wrong key、密文被篡改等）。错误消息不含明文/密文。
    """
    key = _require_master_key_bytes(master_key)
    try:
        ct = base64.b64decode(ciphertext.encode("ascii"), validate=True)
        n = base64.b64decode(nonce.encode("ascii"), validate=True)
        plaintext = AESGCM(key).decrypt(n, ct, None)
    except SecurityConfigurationError:
        raise
    except (ValueError, InvalidTag) as e:
        # ValueError 覆盖 base64 解码失败 / nonce 非法长度；InvalidTag 覆盖 tag 验证失败
        raise SecurityDecryptionError(
            "failed to decrypt api key: bad ciphertext/nonce encoding "
            "or GCM authentication failed"
        ) from e
    return plaintext.decode("utf-8")
