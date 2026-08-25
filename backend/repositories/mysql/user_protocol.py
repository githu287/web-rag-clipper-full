"""
User Repository Protocol（Phase 3.4 Step 3 用户体系）。

【严格阶段约束】
- 仅定义接口（typing.Protocol），不含任何实现代码；
- 不 import sqlalchemy / Engine / Session（具体实现细节在 Impl 中隔离）；
- 不建 engine、不开 session、不执行 SQL；
- 不依赖 FastAPI Depends / Service / API（分层解耦）。

设计风格与 repositories.mysql.protocol.DocumentRepository 完全对齐：
runtime_checkable Protocol + 方法签名 + 行为约束注释，便于上层 Service 用
`repo: UserRepository = Depends(get_user_repository)` 注入并支持 Mock。

范围边界：
- 只负责 users 表数据访问（INSERT / SELECT / UPDATE）；
- 不负责 token 生命周期、register、login、authentication、authorization；
- create_user / update_api_key 接收已经处理好的 hash / ciphertext / nonce，
  不在 Repository 内生成 token / API Key / hash / 加密
  （Security 负责安全计算，Service 负责业务编排，Repository 只负责数据库）。

查询语义（与 Document 族的区别）：
- 本族所有查询方法（get_user_by_token_hash / get_user_by_api_key_hash /
  get_user_by_id）查不到返回 None，不抛 auth 异常、不抛 NotFound；
  401 / 404 的映射由上层 Service / API 负责；
- update_api_key（写操作）对齐 Document 族 update 语义：user_id 不存在抛
  UserNotFoundError。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ...models.user import User


@runtime_checkable
class UserRepository(Protocol):
    """
    users 表数据访问协议（MySQL users 表）。

    实现约束：
    - 不感知 Milvus / Redis / ingest pipeline / 认证流程；
    - 不吞异常：SQLAlchemy 异常统一包装为 core.exceptions.UserRepositoryError
      族后向上抛出；查询不存在返回 None（见模块 docstring）；
    - engine / sessionmaker 由构造函数注入，禁止在 Impl 内硬编码连接参数；
    - 每个方法 = 一次逻辑 CRUD；不做 Service 级编排。
    """

    # ------------------------------------------------------------------ create
    def create_user(
        self,
        api_key_hash: str,
        api_key_ciphertext: str,
        api_key_nonce: str,
        token_hash: str,
    ) -> User:
        """
        插入一条 User 记录。

        新建对象 status 默认 ACTIVE、created_at / updated_at 由 DB 层
        server_default=func.now() 填充。

        Args:
            api_key_hash      : SHA-256(api_key) 十六进制（64 字符，UNIQUE）。
            api_key_ciphertext: AES-256-GCM 密文 + tag 的 Base64（非明文）。
            api_key_nonce     : 该条记录独立的 12B 随机 nonce Base64。
            token_hash        : SHA-256(opaque random token) 十六进制（64 字符，UNIQUE）。

        Returns:
            新建的 User ORM 对象（含自增 id、status=ACTIVE、DB 填充的时间戳）。
            返回对象为 detached（session 已 close），因 expire_on_commit=False
            属性仍可读。

        Raises:
            UserOperationError: SQLAlchemy 执行异常（含 unique(api_key_hash /
                                token_hash) 冲突、连接失败等）。
        """

    # ------------------------------------------------------------ get by token
    def get_user_by_token_hash(self, token_hash: str) -> User | None:
        """
        按 token_hash 精确查询 User（Bearer 认证主路径）。

        Args:
            token_hash: SHA-256(opaque random token) 十六进制。

        Returns:
            User ORM 对象（detached，属性可读）；查不到返回 None（不抛 auth 异常）。

        Raises:
            UserOperationError: SQLAlchemy 执行异常。
        """

    # --------------------------------------------------------- get by api key
    def get_user_by_api_key_hash(self, api_key_hash: str) -> User | None:
        """
        按 api_key_hash 精确查询 User（API Key 唯一识别 / 注册幂等）。

        Args:
            api_key_hash: SHA-256(api_key) 十六进制。

        Returns:
            User ORM 对象（detached，属性可读）；查不到返回 None（不抛 auth 异常）。

        Raises:
            UserOperationError: SQLAlchemy 执行异常。
        """

    # ----------------------------------------------------------------- get by id
    def get_user_by_id(self, user_id: int) -> User | None:
        """
        按主键查询 User。

        Args:
            user_id: users.id 主键。

        Returns:
            User ORM 对象（detached，属性可读）；查不到返回 None。

        Raises:
            UserOperationError: SQLAlchemy 执行异常。
        """

    # ---------------------------------------------------------- update_api_key
    def update_api_key(
        self,
        user_id: int,
        api_key_hash: str,
        api_key_ciphertext: str,
        api_key_nonce: str,
    ) -> User:
        """
        更换用户的 API Key（只更新 api_key_hash / api_key_ciphertext /
        api_key_nonce + updated_at）。

        明确不改变：user_id、token_hash、status（换 Key 时用户身份不变、
        token 不变、知识库归属不变）。

        Args:
            user_id           : 目标用户主键。
            api_key_hash      : 新 API Key 的 SHA-256 十六进制。
            api_key_ciphertext: 新 API Key 的 AES-256-GCM 密文 Base64。
            api_key_nonce     : 新 API Key 的独立随机 nonce Base64。

        Returns:
            更新后的 User ORM 对象（已 commit + refresh，detached 可读）。

        Raises:
            UserNotFoundError : user_id 不存在（对齐 Document 族 update 语义）。
            UserOperationError: SQLAlchemy 执行异常。
        """

    # ------------------------------------------------------------- update_token
    def update_token(self, user_id: int, token_hash: str) -> User:
        """
        轮换用户的 Bearer token（Phase 3.4 Step 4 新增；只更新 token_hash +
        updated_at）。

        触发场景：/auth/login 每次登录成功签发新 token（旧 token 立即失效），
        数据库只保存 hash，Service 无法复用旧 token，故登录必须轮换。

        明确不改变：user_id、api_key_hash / ciphertext / nonce、status
        （token 轮换不改变用户身份与加密的 API Key）。

        Args:
            user_id   : 目标用户主键。
            token_hash: 新 token 的 SHA-256 十六进制。

        Returns:
            更新后的 User ORM 对象（已 commit + refresh，detached 可读）。

        Raises:
            UserNotFoundError : user_id 不存在（对齐 Document 族 update 语义）。
            UserOperationError: SQLAlchemy 执行异常（含 unique(token_hash) 冲突）。
        """
