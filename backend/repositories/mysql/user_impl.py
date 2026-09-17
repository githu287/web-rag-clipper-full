"""
UserRepository 的 SQLAlchemy 实现（Phase 3.4 Step 3）。

设计要点（与 DocumentRepositoryImpl 完全对齐）：
1) 注入 Engine（而非 Settings），便于单元测试用 SQLite 替换 engine；
2) 内部 sessionmaker 用 expire_on_commit=False，保证 commit 后返回的
   detached ORM 对象属性仍可读；
3) Session 生命周期：每个方法内部 `with self._session_factory() as session:`
   管理，方法结束自动 close；
4) 不吞异常：所有 SQLAlchemy 原生异常包装为 UserRepositoryError 族后抛出，
   保留 `raise ... from e` 异常链；
5) 查询方法（get_user_by_token_hash / get_user_by_api_key_hash /
   get_user_by_id）查不到返回 None；update_api_key 不存在抛 UserNotFoundError。

安全红线（Phase 3.4 Step 3）：
- 本模块只接触 hash / ciphertext / nonce，绝不接触 API Key / token 明文；
- 错误消息不包含完整 api_key_hash / token_hash / ciphertext，只保留前 8 位
  哈希前缀用于日志定位。
"""

from __future__ import annotations

from sqlalchemy import Engine, select
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from ...core.exceptions import (
    UserNotFoundError,
    UserOperationError,
)
from ...models.user import User
from .user_protocol import UserRepository


def _hash_prefix(value: str) -> str:
    """日志用哈希前缀（前 8 位 + 长度），避免在错误信息中出现完整 hash。"""
    if not value:
        return "<empty>"
    return f"{value[:8]}... (len={len(value)})"


class UserRepositoryImpl(UserRepository):
    """UserRepository 的 SQLAlchemy 实现。"""

    def __init__(self, engine: Engine) -> None:
        """
        注入 Engine，内部构造 sessionmaker。

        Args:
            engine: SQLAlchemy Engine（生产环境来自 core.db.get_engine；
                    测试环境可传 SQLite engine，如 StaticPool in-memory）。
        """
        self._engine = engine
        # expire_on_commit=False：commit 后不自动 expire，detached ORM 属性仍可读。
        self._session_factory: sessionmaker[Session] = sessionmaker(
            bind=engine,
            expire_on_commit=False,
        )

    # ------------------------------------------------------------------ create
    def create_user(
        self,
        api_key_hash: str,
        api_key_ciphertext: str,
        api_key_nonce: str,
        token_hash: str,
    ) -> User:
        """
        插入一条 User 记录，返回 detached ORM 对象。

        不生成 token / hash / 加密：所有安全计算由上层（Service + Security）
        完成，本方法只负责 INSERT users。
        """
        user = User(
            api_key_hash=api_key_hash,
            api_key_ciphertext=api_key_ciphertext,
            api_key_nonce=api_key_nonce,
            token_hash=token_hash,
        )
        try:
            with self._session_factory() as session:
                session.add(user)
                session.commit()
                session.refresh(user)
                return user
        except (OperationalError, IntegrityError, DBAPIError) as e:
            raise UserOperationError(
                f"create_user failed: "
                f"api_key_hash={_hash_prefix(api_key_hash)}, "
                f"error={e!s}"
            ) from e

    # ------------------------------------------------------------ get by token
    def get_user_by_token_hash(self, token_hash: str) -> User | None:
        """按 token_hash 精确查询；查不到返回 None（不抛 auth 异常）。"""
        try:
            with self._session_factory() as session:
                return (
                    session.execute(
                        select(User).where(User.token_hash == token_hash)
                    )
                    .scalars()
                    .first()
                )
        except (OperationalError, DBAPIError) as e:
            raise UserOperationError(
                f"get_user_by_token_hash failed: error={e!s}"
            ) from e

    # --------------------------------------------------------- get by api key
    def get_user_by_api_key_hash(self, api_key_hash: str) -> User | None:
        """按 api_key_hash 精确查询；查不到返回 None。"""
        try:
            with self._session_factory() as session:
                return (
                    session.execute(
                        select(User).where(User.api_key_hash == api_key_hash)
                    )
                    .scalars()
                    .first()
                )
        except (OperationalError, DBAPIError) as e:
            raise UserOperationError(
                f"get_user_by_api_key_hash failed: error={e!s}"
            ) from e

    # ----------------------------------------------------------------- get by id
    def get_user_by_id(self, user_id: int) -> User | None:
        """按主键查询；查不到返回 None。"""
        try:
            with self._session_factory() as session:
                return session.get(User, user_id)
        except (OperationalError, DBAPIError) as e:
            raise UserOperationError(
                f"get_user_by_id failed: id={user_id}, error={e!s}"
            ) from e

    # ------------------------------------------------------------- update_token
    def update_token(self, user_id: int, token_hash: str) -> User:
        """
        轮换 Bearer token：只更新 token_hash（updated_at 由 ORM onupdate
        自动刷新），不改变 user_id / api_key_* / status。
        """
        try:
            with self._session_factory() as session:
                user = session.get(User, user_id)
                if user is None:
                    raise UserNotFoundError(
                        f"user not found: id={user_id}"
                    )
                user.token_hash = token_hash
                session.commit()
                session.refresh(user)
                return user
        except UserNotFoundError:
            raise
        except (OperationalError, IntegrityError, DBAPIError) as e:
            raise UserOperationError(
                f"update_token failed: id={user_id}, "
                f"token_hash={_hash_prefix(token_hash)}, "
                f"error={e!s}"
            ) from e

    # ---------------------------------------------------------- update_api_key
    def update_api_key(
        self,
        user_id: int,
        api_key_hash: str,
        api_key_ciphertext: str,
        api_key_nonce: str,
    ) -> User:
        """
        更换 API Key：只更新 api_key_hash / api_key_ciphertext / api_key_nonce
        （updated_at 由 ORM onupdate=func.now() 自动刷新）。

        不改变 user_id / token_hash / status。
        """
        try:
            with self._session_factory() as session:
                user = session.get(User, user_id)
                if user is None:
                    raise UserNotFoundError(
                        f"user not found: id={user_id}"
                    )
                user.api_key_hash = api_key_hash
                user.api_key_ciphertext = api_key_ciphertext
                user.api_key_nonce = api_key_nonce
                session.commit()
                session.refresh(user)
                return user
        except UserNotFoundError:
            raise
        except (OperationalError, IntegrityError, DBAPIError) as e:
            raise UserOperationError(
                f"update_api_key failed: id={user_id}, "
                f"api_key_hash={_hash_prefix(api_key_hash)}, "
                f"error={e!s}"
            ) from e
