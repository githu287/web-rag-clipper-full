"""
Plugin Repository Protocol（Phase 3.5 Step 2-B 新增：MySQL plugin_workspaces 表 CRUD）。

【严格阶段约束】
- 仅定义接口（typing.Protocol），不含任何实现代码；
- 不 import sqlalchemy / Engine / Session（具体实现细节在 Impl 中隔离）；
- 不建 engine、不开 session、不执行 SQL；
- 不依赖 FastAPI Depends / Service / API（分层解耦）。

设计风格：runtime_checkable Protocol + 方法签名 + 行为约束注释，便于
PluginService / get_current_plugin 用
`repo: PluginRepository = Depends(get_plugin_repository)` 注入并支持 Mock。

身份模型（Phase 3.5 双凭证，与 backend/models/plugin.py / migration 0007 对齐）：
- plugin_workspaces.id    : 主键（BIGINT PK），仅内部分区用途；
- plugin_id               : Workspace 唯一标识（VARCHAR(64) UNIQUE NOT NULL，**非秘密**，
  对外展示 / documents 归属过滤 / 知识库命名空间）；
- plugin_name             : 展示名称（VARCHAR(64) NOT NULL）；
- plugin_name_norm        : 归一化名称（UNIQUE NOT NULL，「一名一 Workspace」）；
- plugin_secret_hash      : SHA-256(plugin_secret)（CHAR(64) UNIQUE NOT NULL）；
- api_key_ciphertext / api_key_nonce : 百炼模型调用凭证密文 + nonce（NULL = 未配置），
  **绝不参与身份识别**。

Repository 职责边界（Phase 3.5 §7，严格）：
- 只负责 MySQL CRUD（INSERT / SELECT / UPDATE / DELETE）；
- 不生成 plugin_id / plugin_secret；不 hash secret；不 encrypt API Key；
  不归一化 plugin_name；不做 HTTP / 认证 / 业务逻辑；
- create_plugin 接收的 plugin_id / plugin_name / plugin_name_norm /
  plugin_secret_hash / ciphertext / nonce / status 全部视为已经计算好的值，
  Repository 不做任何二次加工。

查询语义（与 User / Document 族对齐）：
- 所有 get_* 查询方法查不到返回 None（不抛 auth / NotFound 异常，
  401 / 404 映射由上层 Service 决定）；
- update / clear / delete 类写操作目标不存在抛 PluginNotFoundError。

字段修改边界：
- update_plugin_name : 只允许改 plugin_name / plugin_name_norm（+updated_at）；
- update_api_key      : 只允许改 api_key_ciphertext / api_key_nonce（+updated_at）；
- clear_api_key       : 只允许置 api_key_ciphertext / api_key_nonce 为 NULL；
- update_status       : 只允许改 status；
- delete_plugin       : 删除整个 workspace 行（本阶段只删 MySQL 行；
  跨系统清理编排属 PluginService 职责，Repository 不实现）；
- 任何方法均禁止修改 id / plugin_id / plugin_secret_hash。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ...models.plugin import PluginWorkspace


@runtime_checkable
class PluginRepository(Protocol):
    """
    plugin_workspaces 表数据访问协议（MySQL plugin_workspaces 表）。

    实现约束：
    - 不感知 Milvus / Redis / ingest pipeline / 认证流程；
    - 不吞异常：SQLAlchemy 异常统一包装为 core.exceptions.PluginRepositoryError
      族后向上抛出；查询不存在返回 None（见模块 docstring）；
    - engine / sessionmaker 由构造函数注入，禁止在 Impl 内硬编码连接参数；
    - 每个方法 = 一次逻辑 CRUD；不做 Service 级编排；
    - Repository 不负责任何 plugin_id 生成 / secret hash / API Key 加密 /
      name 归一化 / 身份判断。
    """

    # ------------------------------------------------------------------ create
    def create_plugin(
        self,
        plugin_id: str,
        plugin_name: str,
        plugin_name_norm: str,
        plugin_secret_hash: str,
        api_key_ciphertext: str | None = None,
        api_key_nonce: str | None = None,
        status: str = "ACTIVE",
    ) -> PluginWorkspace:
        """
        插入一条 Plugin Workspace 记录。

        所有参数视为已经计算好的值（生成 / 哈希 / 加密由上层 Security /
        PluginService 完成）；api_key_ciphertext / api_key_nonce 缺省或 None
        时保持 NULL（未配置模型 Key）。status 缺省 "ACTIVE"。

        Args:
            plugin_id          : Workspace 唯一标识（VARCHAR(64)，UNIQUE，非秘密）。
            plugin_name        : 展示名称（VARCHAR(64)）。
            plugin_name_norm   : 归一化名称（strip → 连续空白压缩 → lower，UNIQUE）。
            plugin_secret_hash : SHA-256(plugin_secret) 十六进制（64 字符）。
            api_key_ciphertext : AES-256-GCM 密文 + tag 的 Base64（非明文）；
                                 可传 None / 缺省（未配置模型 Key）。
            api_key_nonce      : 该条记录独立的 12B 随机 nonce Base64；可传 None / 缺省。
            status             : ACTIVE / DISABLED / DELETING，缺省 ACTIVE。

        Returns:
            新建的 PluginWorkspace ORM 对象（含自增 id、DB 填充的时间戳；
            detached 可读，因 expire_on_commit=False）。

        Raises:
            PluginOperationError: SQLAlchemy 执行异常（含 unique(plugin_id /
                                  plugin_name_norm / plugin_secret_hash) 冲突等）。
        """

    # ---------------------------------------------------------- get by plugin_id
    def get_by_plugin_id(self, plugin_id: str) -> PluginWorkspace | None:
        """
        按 plugin_id 精确查询（认证 / 归属过滤主路径）。

        Args:
            plugin_id: Workspace 唯一标识（UNIQUE）。

        Returns:
            PluginWorkspace ORM 对象（detached，属性可读）；查不到返回 None。

        Raises:
            PluginOperationError: SQLAlchemy 执行异常。
        """

    # ------------------------------------------------------- get by name norm
    def get_by_plugin_name_norm(self, plugin_name_norm: str) -> PluginWorkspace | None:
        """
        按归一化名称精确查询（注册防重名主路径）。

        Args:
            plugin_name_norm: 归一化名称（strip → 连续空白压缩 → lower，UNIQUE）。

        Returns:
            PluginWorkspace ORM 对象（detached，属性可读）；查不到返回 None。

        Raises:
            PluginOperationError: SQLAlchemy 执行异常。
        """

    # ------------------------------------------------------- get by secret hash
    def get_by_secret_hash(self, plugin_secret_hash: str) -> PluginWorkspace | None:
        """
        按 secret 哈希精确查询（plugin_id + plugin_secret 双凭证认证路径）。

        Args:
            plugin_secret_hash: SHA-256(plugin_secret) 十六进制（64 字符）。

        Returns:
            PluginWorkspace ORM 对象（detached，属性可读）；查不到返回 None
            （不抛 auth 异常，401 映射由上层 Service 决定）。

        Raises:
            PluginOperationError: SQLAlchemy 执行异常。
        """

    # ----------------------------------------------------------------- get by id
    def get_by_id(self, plugin_workspace_id: int) -> PluginWorkspace | None:
        """
        按主键查询。

        Args:
            plugin_workspace_id: plugin_workspaces.id 主键。

        Returns:
            PluginWorkspace ORM 对象（detached，属性可读）；查不到返回 None。

        Raises:
            PluginOperationError: SQLAlchemy 执行异常。
        """

    # --------------------------------------------------------- update_plugin_name
    def update_plugin_name(
        self,
        plugin_id: str,
        plugin_name: str,
        plugin_name_norm: str,
    ) -> PluginWorkspace:
        """
        更新展示名与归一化名（只更新 plugin_name / plugin_name_norm + updated_at）。

        明确不改变：id、plugin_id、plugin_secret_hash、api_key_ciphertext /
        api_key_nonce、status（改名不影响身份 / 凭证 / 知识库归属）。

        Args:
            plugin_id        : 目标 Workspace 标识。
            plugin_name      : 新展示名。
            plugin_name_norm : 新归一化名。

        Returns:
            更新后的 PluginWorkspace ORM 对象（已 commit + refresh，detached 可读）。

        Raises:
            PluginNotFoundError : plugin_id 不存在（对齐 Document 族 update 语义）。
            PluginOperationError: SQLAlchemy 执行异常（含 unique(plugin_name_norm)
                                 冲突）。
        """

    # ------------------------------------------------------------- update_api_key
    def update_api_key(
        self,
        plugin_id: str,
        api_key_ciphertext: str,
        api_key_nonce: str,
    ) -> PluginWorkspace:
        """
        更换模型的 API Key（只更新 api_key_ciphertext / api_key_nonce + updated_at）。

        明确不改变：id、plugin_id、plugin_name / plugin_name_norm、
        plugin_secret_hash、status（换 Key 时 Workspace 身份不变、归属不变）。

        Args:
            plugin_id          : 目标 Workspace 标识。
            api_key_ciphertext : 新 API Key 的 AES-256-GCM 密文 Base64。
            api_key_nonce      : 新 API Key 的独立随机 nonce Base64。

        Returns:
            更新后的 PluginWorkspace ORM 对象（已 commit + refresh，detached 可读）。

        Raises:
            PluginNotFoundError : plugin_id 不存在。
            PluginOperationError: SQLAlchemy 执行异常。
        """

    # --------------------------------------------------------------- clear_api_key
    def clear_api_key(self, plugin_id: str) -> PluginWorkspace:
        """
        清除模型的 API Key（只设置 api_key_ciphertext / api_key_nonce = NULL
        + updated_at）。

        明确不改变：id、plugin_id、plugin_name / plugin_name_norm、
        plugin_secret_hash、status 及 documents 归属（清除 Key 只影响模型调用
        能力，不产生任何数据 / 身份副作用）。

        Args:
            plugin_id: 目标 Workspace 标识。

        Returns:
            更新后的 PluginWorkspace ORM 对象（已 commit + refresh，detached 可读）。

        Raises:
            PluginNotFoundError : plugin_id 不存在。
            PluginOperationError: SQLAlchemy 执行异常。
        """

    # --------------------------------------------------------------- update_status
    def update_status(self, plugin_id: str, status: str) -> PluginWorkspace:
        """
        更新 Workspace 状态（ACTIVE / DISABLED / DELETING）。

        明确不改变：id、plugin_id、plugin_name / plugin_name_norm、
        plugin_secret_hash、api_key_ciphertext / api_key_nonce。

        Args:
            plugin_id: 目标 Workspace 标识。
            status   : PluginStatus.ALL 中的合法值。

        Returns:
            更新后的 PluginWorkspace ORM 对象（已 commit + refresh，detached 可读）。

        Raises:
            PluginNotFoundError : plugin_id 不存在。
            PluginOperationError: 非 PluginStatus.ALL 的非法 status，或
                                  SQLAlchemy 执行异常。
        """

    # ------------------------------------------------------------------ delete
    def delete_plugin(self, plugin_id: str) -> PluginWorkspace:
        """
        删除一个 Plugin Workspace（本阶段只删除 MySQL plugin_workspaces 行）。

        跨系统清理（Milvus 数据 / documents 级联 / 凭证轮换等）编排属
        PluginService 职责，Repository 不实现。删除前如需校验不残留 documents，
        属 Service 编排，本方法不承担。

        Args:
            plugin_id: 目标 Workspace 标识。

        Returns:
            被删除的 PluginWorkspace ORM 对象（detached 可读）。

        Raises:
            PluginNotFoundError : plugin_id 不存在。
            PluginOperationError: SQLAlchemy 执行异常。
        """
