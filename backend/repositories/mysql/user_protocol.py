"""
User Repository Protocol（Phase 3.4 Step 3 用户体系；F-REV3 身份重构）。

【严格阶段约束】
- 仅定义接口（typing.Protocol），不含任何实现代码；
- 不 import sqlalchemy / Engine / Session（具体实现细节在 Impl 中隔离）；
- 不建 engine、不开 session、不执行 SQL；
- 不依赖 FastAPI Depends / Service / API（分层解耦）。

设计风格与 repositories.mysql.protocol.DocumentRepository 完全对齐：
runtime_checkable Protocol + 方法签名 + 行为约束注释，便于上层 Service 用
`repo: UserRepository = Depends(get_user_repository)` 注入并支持 Mock。

身份模型（F-REV3 最终版，与 backend/models/user.py 对齐）：
- users.id            : 唯一主身份，永不变；documents.user_id 指向它；
- username            : 身份入口（UNIQUE NOT NULL），注册 / 登录凭据；
- password_hash       : 登录凭证（Argon2id PHC，NOT NULL）；
- token_hash          : 会话认证（UNIQUE，NULL = 未登录 / 已 logout）；
- api_key_ciphertext / api_key_nonce : 百炼模型调用凭证（NULL = 未配置）；
- API Key 完全退出身份体系：Repository 不存在任何按 API Key 查询的方法。

范围边界：
- 只负责 users 表数据访问（INSERT / SELECT / UPDATE）；
- 不负责 password 哈希、API Key 加密、token 生成、认证编排
  （Security 负责安全计算，Service 负责业务编排，Repository 只负责数据库）；
- create_user / update_api_key / update_token 接收已经处理好的
  password_hash / ciphertext / nonce / token_hash，Repository 不生成任何字段。

查询语义（与 Document 族的区别）：
- 所有查询方法（get_user_by_username / get_user_by_token_hash /
  get_user_by_id）查不到返回 None，不抛 auth 异常、不抛 NotFound；
  401 / 404 的映射由上层 Service / API 负责；
- update / clear 类写操作对齐 Document 族 update 语义：user_id 不存在抛
  UserNotFoundError。

字段修改边界：
- update_api_key  只允许改 api_key_ciphertext / api_key_nonce（+updated_at）；
- clear_api_key   只允许置 api_key_ciphertext / api_key_nonce 为 NULL；
- update_token    只允许改 token_hash（可为 None）；
- clear_token     只允许置 token_hash 为 NULL；
- 任何方法均禁止修改 id / username / password_hash / status。
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
    - 每个方法 = 一次逻辑 CRUD；不做 Service 级编排；
    - Repository 不负责任何 hash / 加密 / token 生成 / 身份判断。
    """

    # ------------------------------------------------------------------ create
    def create_user(
        self,
        username: str,
        password_hash: str,
        token_hash: str | None,
        api_key_ciphertext: str | None = None,
        api_key_nonce: str | None = None,
    ) -> User:
        """
        插入一条 User 记录。

        新建对象 status 默认 ACTIVE、created_at / updated_at 由 DB 层
        server_default=func.now() 填充。Repository 不生成任何字段：
        password_hash / token_hash / ciphertext / nonce 全部由上层计算后传入。

        Args:
            username          : 身份入口（VARCHAR(64)，UNIQUE）。
            password_hash     : Argon2id PHC 字符串（core.security.hash_password）。
            token_hash        : SHA-256(opaque random token) 十六进制；
                                可传 None（未登录状态）。
            api_key_ciphertext: AES-256-GCM 密文 + tag 的 Base64（非明文）；
                                可传 None / 缺省（未配置模型 Key）。
            api_key_nonce     : 该条记录独立的 12B 随机 nonce Base64；
                                可传 None / 缺省。

        Returns:
            新建的 User ORM 对象（含自增 id、status=ACTIVE、DB 填充的时间戳）。
            返回对象为 detached（session 已 close），因 expire_on_commit=False
            属性仍可读。

        Raises:
            UserOperationError: SQLAlchemy 执行异常（含 unique(username /
                                token_hash) 冲突、连接失败等）。
        """

    # ----------------------------------------------------------- get by name
    def get_user_by_username(self, username: str) -> User | None:
        """
        按 username 精确查询 User（注册 / 登录主路径）。

        Args:
            username: 身份入口（UNIQUE）。

        Returns:
            User ORM 对象（detached，属性可读）；查不到返回 None（不抛异常）。
            登录语义（不存在 vs 密码错）由上层 Service 统一为 InvalidCredentialsError。

        Raises:
            UserOperationError: SQLAlchemy 执行异常。
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
        api_key_ciphertext: str,
        api_key_nonce: str,
    ) -> User:
        """
        更换用户的模型 API Key（只更新 api_key_ciphertext / api_key_nonce +
        updated_at）。

        明确不改变：user_id、username、password_hash、token_hash、status
        （换 Key 时用户身份不变、token 不变、知识库归属不变）。

        Args:
            user_id           : 目标用户主键。
            api_key_ciphertext: 新 API Key 的 AES-256-GCM 密文 Base64。
            api_key_nonce     : 新 API Key 的独立随机 nonce Base64。

        Returns:
            更新后的 User ORM 对象（已 commit + refresh，detached 可读）。

        Raises:
            UserNotFoundError : user_id 不存在（对齐 Document 族 update 语义）。
            UserOperationError: SQLAlchemy 执行异常。
        """

    # ----------------------------------------------------------- clear_api_key
    def clear_api_key(self, user_id: int) -> User:
        """
        清除用户的模型 API Key（只设置 api_key_ciphertext / api_key_nonce =
        NULL + updated_at）。

        明确不改变：user_id、username、password_hash、token_hash、status
        及 documents 归属（清除 Key 只影响模型调用能力，不产生任何
        数据/身份副作用）。

        Args:
            user_id: 目标用户主键。

        Returns:
            更新后的 User ORM 对象（已 commit + refresh，detached 可读）。

        Raises:
            UserNotFoundError : user_id 不存在。
            UserOperationError: SQLAlchemy 执行异常。
        """

    # ------------------------------------------------------------- update_token
    def update_token(
        self,
        user_id: int,
        token_hash: str | None,
    ) -> User:
        """
        写入 / 轮换用户的会话 token（只更新 token_hash + updated_at）。

        触发场景：/auth/login 每次登录成功签发新 token（旧 token 立即失效）；
        数据库只保存 hash，Service 无法复用旧 token，故登录必须轮换。

        token_hash 传 None 等价于清除会话（clear_token 的通用形态），
        但语义上推荐使用 clear_token 表达显式登出。

        明确不改变：user_id、username、password_hash、api_key_ciphertext /
        api_key_nonce、status（token 不参与 API Key 与身份逻辑）。

        Args:
            user_id   : 目标用户主键。
            token_hash: 新 token 的 SHA-256 十六进制，或 None（清除）。

        Returns:
            更新后的 User ORM 对象（已 commit + refresh，detached 可读）。

        Raises:
            UserNotFoundError : user_id 不存在（对齐 Document 族 update 语义）。
            UserOperationError: SQLAlchemy 执行异常（含 unique(token_hash) 冲突）。
        """

    # ------------------------------------------------------------- clear_token
    def clear_token(self, user_id: int) -> User:
        """
        清除用户的会话 token（只设置 token_hash = NULL + updated_at）。

        本质等价于 update_token(user_id, None)；作为显式登出语义保留，
        便于调用方表达「结束会话」。API Key 与 username 完全不参与本逻辑。

        明确不改变：user_id、username、password_hash、api_key_ciphertext /
        api_key_nonce、status。

        Args:
            user_id: 目标用户主键。

        Returns:
            更新后的 User ORM 对象（已 commit + refresh，detached 可读）。

        Raises:
            UserNotFoundError : user_id 不存在。
            UserOperationError: SQLAlchemy 执行异常。
        """
