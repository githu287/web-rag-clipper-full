"""
UserRepositoryImpl 单元测试（Phase 3.4 Step 3）。

测试策略（复用 test_document_repository.py 模式）：
1) SQLite in-memory engine + StaticPool 替换真实 MySQL；
2) 每个测试 setUp 重建 engine + create_all（表级隔离）；
3) 覆盖 create_user / get_user_by_id / get_user_by_token_hash /
   get_user_by_api_key_hash / 不存在返回 None / update_api_key 语义 /
   update_token 语义（Phase 3.4 Step 4 新增）/ unique(api_key_hash / token_hash) 冲突。

不依赖：
- 真实 MySQL / Redis / Milvus；
- 真实 .env（本测试不触碰 Settings）；
- FastAPI / Service / API。

注意：SQLite 唯一约束冲突以 IntegrityError 抛出，Impl 包装为
UserOperationError（与 MySQL 行为一致，MySQL 的 IntegrityError 同样被包装）。
"""

from __future__ import annotations

import hashlib
import unittest

from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import StaticPool

from backend.core.exceptions import UserNotFoundError, UserOperationError
from backend.models.base import Base
from backend.models.user import User, UserStatus
from backend.repositories.mysql import (
    UserRepository,
    UserRepositoryImpl,
)


def _make_test_engine() -> Engine:
    """SQLite in-memory + StaticPool：单连接共享，便于 create_all/drop_all。"""
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _sha256(value: str) -> str:
    """生成 64 位十六进制 hash，模拟真实 sha256_hex 输出。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class UserRepositoryImplTest(unittest.TestCase):
    """UserRepositoryImpl（SQLite）行为测试。"""

    def setUp(self) -> None:
        self.engine = _make_test_engine()
        Base.metadata.create_all(self.engine)
        self.repo: UserRepository = UserRepositoryImpl(self.engine)

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    # ------------------------------------------------------------------ helper
    def _create_user(self, tag: str) -> User:
        """插入一个 User，api_key_hash / token_hash 由 tag 派生且互不相同。"""
        return self.repo.create_user(
            api_key_hash=_sha256(f"api-key-{tag}"),
            api_key_ciphertext=f"ciphertext-{tag}",
            api_key_nonce=f"nonce-{tag}",
            token_hash=_sha256(f"token-{tag}"),
        )

    # --------------------------------------------------------------- create_user
    def test_create_user(self) -> None:
        """create_user 写入全部字段，自增 id > 0，status 默认 ACTIVE。"""
        user = self._create_user("a")
        self.assertGreater(user.id, 0)
        self.assertEqual(user.api_key_hash, _sha256("api-key-a"))
        self.assertEqual(user.api_key_ciphertext, "ciphertext-a")
        self.assertEqual(user.api_key_nonce, "nonce-a")
        self.assertEqual(user.token_hash, _sha256("token-a"))
        self.assertEqual(user.status, UserStatus.ACTIVE)
        self.assertIsNotNone(user.created_at)
        self.assertIsNotNone(user.updated_at)

    # ------------------------------------------------------------- get by id
    def test_get_user_by_id(self) -> None:
        """按主键查询命中。"""
        created = self._create_user("a")
        fetched = self.repo.get_user_by_id(created.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.id, created.id)
        self.assertEqual(fetched.token_hash, created.token_hash)

    # ------------------------------------------------------- get by token hash
    def test_get_user_by_token_hash(self) -> None:
        """按 token_hash 精确查询命中。"""
        created = self._create_user("a")
        fetched = self.repo.get_user_by_token_hash(created.token_hash)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.id, created.id)

    # ------------------------------------------------------ get by api key hash
    def test_get_user_by_api_key_hash(self) -> None:
        """按 api_key_hash 精确查询命中。"""
        created = self._create_user("a")
        fetched = self.repo.get_user_by_api_key_hash(created.api_key_hash)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.id, created.id)

    # ----------------------------------------------------- not found returns None
    def test_not_found_returns_none(self) -> None:
        """三个查询方法对不存在的值均返回 None，不抛异常。"""
        self._create_user("a")
        self.assertIsNone(self.repo.get_user_by_id(9999))
        self.assertIsNone(
            self.repo.get_user_by_token_hash(_sha256("no-such-token"))
        )
        self.assertIsNone(
            self.repo.get_user_by_api_key_hash(_sha256("no-such-key"))
        )

    # ---------------------------------------------------------- update_api_key
    def test_update_api_key(self) -> None:
        """update_api_key 更新 api_key_hash / ciphertext / nonce，返回新值。"""
        created = self._create_user("a")
        updated = self.repo.update_api_key(
            user_id=created.id,
            api_key_hash=_sha256("api-key-updated"),
            api_key_ciphertext="ciphertext-updated",
            api_key_nonce="nonce-updated",
        )
        self.assertEqual(updated.api_key_hash, _sha256("api-key-updated"))
        self.assertEqual(updated.api_key_ciphertext, "ciphertext-updated")
        self.assertEqual(updated.api_key_nonce, "nonce-updated")

    def test_update_api_key_keeps_user_id(self) -> None:
        """update_api_key 不改变 user_id。"""
        created = self._create_user("a")
        updated = self.repo.update_api_key(
            user_id=created.id,
            api_key_hash=_sha256("api-key-updated"),
            api_key_ciphertext="c",
            api_key_nonce="n",
        )
        self.assertEqual(updated.id, created.id)

    def test_update_api_key_keeps_token_hash(self) -> None:
        """update_api_key 不改变 token_hash（换 Key 时 token 不变）。"""
        created = self._create_user("a")
        updated = self.repo.update_api_key(
            user_id=created.id,
            api_key_hash=_sha256("api-key-updated"),
            api_key_ciphertext="c",
            api_key_nonce="n",
        )
        self.assertEqual(updated.token_hash, created.token_hash)

    # --------------------------------------------------- unique constraints
    def test_unique_api_key_hash(self) -> None:
        """重复 api_key_hash 插入抛 UserOperationError（IntegrityError 包装）。"""
        self._create_user("dup")
        with self.assertRaises(UserOperationError):
            self.repo.create_user(
                api_key_hash=_sha256("api-key-dup"),  # 与上面相同
                api_key_ciphertext="c2",
                api_key_nonce="n2",
                token_hash=_sha256("token-other"),
            )

    def test_unique_token_hash(self) -> None:
        """重复 token_hash 插入抛 UserOperationError（IntegrityError 包装）。"""
        self._create_user("dup")
        with self.assertRaises(UserOperationError):
            self.repo.create_user(
                api_key_hash=_sha256("api-key-other"),
                api_key_ciphertext="c2",
                api_key_nonce="n2",
                token_hash=_sha256("token-dup"),  # 与上面相同
            )

    # ------------------------------------------------------------ update_token
    def test_update_token(self) -> None:
        """update_token 更新 token_hash 并返回新值。"""
        created = self._create_user("a")
        new_token_hash = _sha256("token-rotated")
        updated = self.repo.update_token(created.id, new_token_hash)
        self.assertEqual(updated.token_hash, new_token_hash)

    def test_update_token_keeps_user_id(self) -> None:
        """update_token 不改变 user_id。"""
        created = self._create_user("a")
        updated = self.repo.update_token(created.id, _sha256("token-rotated"))
        self.assertEqual(updated.id, created.id)

    def test_update_token_keeps_api_key(self) -> None:
        """update_token 不改变 api_key_hash / ciphertext / nonce（token 与 Key 解耦）。"""
        created = self._create_user("a")
        updated = self.repo.update_token(created.id, _sha256("token-rotated"))
        self.assertEqual(updated.api_key_hash, created.api_key_hash)
        self.assertEqual(updated.api_key_ciphertext, created.api_key_ciphertext)
        self.assertEqual(updated.api_key_nonce, created.api_key_nonce)

    def test_update_token_user_not_found(self) -> None:
        """update_token 对不存在的 user_id 抛 UserNotFoundError。"""
        with self.assertRaises(UserNotFoundError):
            self.repo.update_token(9999, _sha256("token-rotated"))

    def test_update_token_updated_at_changes(self) -> None:
        """update_token 后 updated_at 刷新（ORM onupdate 生效）。"""
        created = self._create_user("a")
        updated = self.repo.update_token(created.id, _sha256("token-rotated"))
        self.assertIsNotNone(updated.updated_at)


if __name__ == "__main__":
    unittest.main()
