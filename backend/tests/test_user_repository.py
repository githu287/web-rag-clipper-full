"""
UserRepository 专项测试（Phase 3.4 Step 3；F-REV3 身份重构）。

覆盖范围（对应 F3 测试要求 A~G）：
    A. create_user：完整创建 / 无 API Key（ciphertext/nonce=NULL）/ 无 token；
    B. username：按 username 查询、不存在返回 None、唯一约束；
    C. token：按 token_hash 查询、NULL 语义、update_token 覆盖、clear_token；
    D. API Key：update_api_key 写入且不改变 user_id/username/password_hash/
       token_hash；clear_api_key 置 NULL 且不影响身份与 documents 所有权；
    E. user_id：get_user_by_id、不存在返回 None、update/clear 不存在抛
       UserNotFoundError；
    F. 安全：仅使用伪造 hash/ciphertext/token，错误消息不含敏感值；
    G. 旧身份能力已删除：get_user_by_api_key_hash 在 Protocol / Impl 均不存在。

环境：
    - 仅使用项目 .venv；SQLite in-memory（StaticPool）+ Base.metadata.create_all，
      不连接真实 MySQL，不执行 alembic；
    - 所有 hash / ciphertext / token 均为伪造字符串，绝无真实 API Key。
"""

import hashlib
import unittest

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.core.exceptions import (
    UserNotFoundError,
    UserOperationError,
)
from backend.models.base import Base
import backend.models.document  # noqa: F401  确保 Document 注册到 metadata
import backend.models.user  # noqa: F401  确保 User 注册到 metadata
from backend.models.document import Document, DocumentSourceType, DocumentStatus
from backend.models.user import User, UserStatus
from backend.repositories.mysql.user_impl import UserRepositoryImpl
from backend.repositories.mysql.user_protocol import UserRepository


class TestUserRepository(unittest.TestCase):
    """UserRepositoryImpl 单元测试（SQLite in-memory）。"""

    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(cls.engine)
        cls.repo = UserRepositoryImpl(cls.engine)

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(cls.engine)
        cls.engine.dispose()

    def setUp(self):
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM documents"))
            conn.execute(text("DELETE FROM users"))

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _fake_argon2(tag: str) -> str:
        """伪造 Argon2id PHC 格式串（不可验证，仅用于测试字段流转）。"""
        return f"$argon2id$v=19$m=65536,t=3,p=4${tag}-salt${tag}-hash"

    @staticmethod
    def _fake_hash(tag: str) -> str:
        """伪造 SHA-256 十六进制（64 字符），模拟 token_hash。"""
        return hashlib.sha256(tag.encode("utf-8")).hexdigest()

    def _create_user(
        self,
        username: str = "alice",
        with_token: bool = True,
        with_api_key: bool = True,
    ) -> User:
        return self.repo.create_user(
            username=username,
            password_hash=self._fake_argon2(f"pw-{username}"),
            token_hash=self._fake_hash(f"tok-{username}") if with_token else None,
            api_key_ciphertext=f"ciphertext-{username}" if with_api_key else None,
            api_key_nonce=f"nonce-{username}" if with_api_key else None,
        )

    # ------------------------------------------------------------ A. create
    def test_create_user_with_all_fields(self):
        user = self._create_user()
        self.assertGreater(user.id, 0)
        self.assertEqual(user.username, "alice")
        self.assertEqual(user.password_hash, self._fake_argon2("pw-alice"))
        self.assertEqual(user.token_hash, self._fake_hash("tok-alice"))
        self.assertEqual(user.api_key_ciphertext, "ciphertext-alice")
        self.assertEqual(user.api_key_nonce, "nonce-alice")
        self.assertEqual(user.status, UserStatus.ACTIVE)

    def test_create_user_without_api_key(self):
        user = self._create_user(with_api_key=False)
        self.assertIsNone(user.api_key_ciphertext)
        self.assertIsNone(user.api_key_nonce)

    def test_create_user_without_token(self):
        user = self._create_user(with_token=False)
        self.assertIsNone(user.token_hash)

    # ------------------------------------------------------------- B. username
    def test_get_user_by_username_found(self):
        created = self._create_user()
        found = self.repo.get_user_by_username("alice")
        self.assertIsNotNone(found)
        self.assertEqual(found.id, created.id)
        self.assertEqual(found.username, "alice")

    def test_get_user_by_username_not_found(self):
        self.assertIsNone(self.repo.get_user_by_username("nobody"))

    def test_username_unique_constraint(self):
        self._create_user(username="alice")
        with self.assertRaises(UserOperationError):
            self._create_user(username="alice")

    # ---------------------------------------------------------------- C. token
    def test_get_user_by_token_hash_found(self):
        created = self._create_user()
        found = self.repo.get_user_by_token_hash(self._fake_hash("tok-alice"))
        self.assertIsNotNone(found)
        self.assertEqual(found.id, created.id)

    def test_get_user_by_token_hash_not_found(self):
        self.assertIsNone(
            self.repo.get_user_by_token_hash(self._fake_hash("tok-nobody"))
        )

    def test_token_hash_nullable(self):
        user = self._create_user(with_token=False)
        self.assertIsNone(user.token_hash)

    def test_update_token_overwrites(self):
        user = self._create_user()
        new_hash = self._fake_hash("tok-alice-rotated")
        updated = self.repo.update_token(user.id, new_hash)
        self.assertEqual(updated.token_hash, new_hash)
        # 旧 token 立即失效，新 token 可认证
        self.assertIsNone(
            self.repo.get_user_by_token_hash(self._fake_hash("tok-alice"))
        )
        self.assertIsNotNone(self.repo.get_user_by_token_hash(new_hash))

    def test_update_token_accepts_none(self):
        user = self._create_user()
        updated = self.repo.update_token(user.id, None)
        self.assertIsNone(updated.token_hash)

    def test_clear_token(self):
        user = self._create_user()
        cleared = self.repo.clear_token(user.id)
        self.assertIsNone(cleared.token_hash)
        # 会话立即失效
        self.assertIsNone(
            self.repo.get_user_by_token_hash(self._fake_hash("tok-alice"))
        )

    def test_clear_token_keeps_identity(self):
        user = self._create_user()
        cleared = self.repo.clear_token(user.id)
        self.assertEqual(cleared.id, user.id)
        self.assertEqual(cleared.username, "alice")
        self.assertEqual(cleared.password_hash, user.password_hash)
        self.assertEqual(cleared.api_key_ciphertext, user.api_key_ciphertext)

    # ---------------------------------------------------------------- D. API Key
    def test_update_api_key(self):
        user = self._create_user()
        updated = self.repo.update_api_key(user.id, "ciphertext-new", "nonce-new")
        self.assertEqual(updated.api_key_ciphertext, "ciphertext-new")
        self.assertEqual(updated.api_key_nonce, "nonce-new")

    def test_update_api_key_keeps_user_id(self):
        user = self._create_user()
        updated = self.repo.update_api_key(user.id, "ciphertext-new", "nonce-new")
        self.assertEqual(updated.id, user.id)

    def test_update_api_key_keeps_username(self):
        user = self._create_user()
        updated = self.repo.update_api_key(user.id, "ciphertext-new", "nonce-new")
        self.assertEqual(updated.username, "alice")

    def test_update_api_key_keeps_password_hash(self):
        user = self._create_user()
        updated = self.repo.update_api_key(user.id, "ciphertext-new", "nonce-new")
        self.assertEqual(updated.password_hash, user.password_hash)

    def test_update_api_key_keeps_token_hash(self):
        user = self._create_user()
        updated = self.repo.update_api_key(user.id, "ciphertext-new", "nonce-new")
        self.assertEqual(updated.token_hash, user.token_hash)

    def test_clear_api_key(self):
        user = self._create_user()
        cleared = self.repo.clear_api_key(user.id)
        self.assertIsNone(cleared.api_key_ciphertext)
        self.assertIsNone(cleared.api_key_nonce)

    def test_clear_api_key_keeps_identity_and_documents(self):
        user = self._create_user()
        with Session(self.engine) as session:
            session.add(
                Document(
                    user_id=user.id,
                    filename="note.txt",
                    file_path="/tmp/note.txt",
                    file_size=123,
                    mime_type="text/plain",
                    status=DocumentStatus.SUCCESS,
                    source_type=DocumentSourceType.UPLOAD,
                )
            )
            session.commit()
        cleared = self.repo.clear_api_key(user.id)
        # 身份字段不变
        self.assertEqual(cleared.id, user.id)
        self.assertEqual(cleared.username, "alice")
        self.assertEqual(cleared.password_hash, user.password_hash)
        self.assertEqual(cleared.token_hash, user.token_hash)
        # documents 所有权不变
        with Session(self.engine) as session:
            docs = (
                session.execute(
                    select(Document).where(Document.user_id == user.id)
                )
                .scalars()
                .all()
            )
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].user_id, user.id)

    # ---------------------------------------------------------------- E. user_id
    def test_get_user_by_id_found(self):
        created = self._create_user()
        found = self.repo.get_user_by_id(created.id)
        self.assertIsNotNone(found)
        self.assertEqual(found.id, created.id)
        self.assertEqual(found.username, "alice")

    def test_get_user_by_id_not_found(self):
        self.assertIsNone(self.repo.get_user_by_id(999999))

    def test_update_api_key_user_not_found(self):
        with self.assertRaises(UserNotFoundError):
            self.repo.update_api_key(999999, "ciphertext-x", "nonce-x")

    def test_update_token_user_not_found(self):
        with self.assertRaises(UserNotFoundError):
            self.repo.update_token(999999, self._fake_hash("tok-x"))

    def test_clear_api_key_user_not_found(self):
        with self.assertRaises(UserNotFoundError):
            self.repo.clear_api_key(999999)

    def test_clear_token_user_not_found(self):
        with self.assertRaises(UserNotFoundError):
            self.repo.clear_token(999999)

    # ---------------------------------------------------------------- F. security
    def test_fake_secrets_only(self):
        # 全部测试仅使用伪造 hash / ciphertext / token（见 _fake_argon2 /
        # _fake_hash / 前缀字符串），确保真实 API Key / token / 密码永不进入
        # 测试数据或错误消息。
        self.assertTrue(self._fake_argon2("pw-alice").startswith("$argon2id$"))
        self.assertEqual(len(self._fake_hash("tok-alice")), 64)

    def test_error_message_does_not_leak_secrets(self):
        pw = self._fake_argon2("pw-alice")
        tok = self._fake_hash("tok-alice")
        self.repo.create_user("alice", pw, tok)
        with self.assertRaises(UserOperationError) as cm:
            self.repo.create_user(
                "alice",
                self._fake_argon2("pw-alice-2"),
                self._fake_hash("tok-alice-2"),
            )
        msg = str(cm.exception)
        self.assertIn("alice", msg)  # username 可作定位信息
        self.assertNotIn(pw, msg)  # password_hash 全文不得泄漏
        self.assertNotIn(tok, msg)  # token_hash 全文不得泄漏
        self.assertNotIn("ciphertext", msg)  # ciphertext 不进入错误消息

    # ------------------------------------------------------------------ G. removed
    def test_old_api_key_query_removed(self):
        # 旧身份能力已删除：Protocol / Impl / 实例均不得再暴露
        # get_user_by_api_key_hash（API Key 退出身份体系）。
        self.assertFalse(hasattr(self.repo, "get_user_by_api_key_hash"))
        self.assertFalse(hasattr(UserRepository, "get_user_by_api_key_hash"))
        self.assertFalse(hasattr(UserRepositoryImpl, "get_user_by_api_key_hash"))


if __name__ == "__main__":
    unittest.main()
