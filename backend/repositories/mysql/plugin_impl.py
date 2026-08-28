"""
PluginRepository 的 SQLAlchemy 实现（Phase 3.5 Step 2-B 新增）。

设计要点（与 DocumentRepositoryImpl 对齐）：
1) 注入 Engine（而非 Settings），便于单元测试用 SQLite 替换 engine；
2) 内部 sessionmaker 用 expire_on_commit=False，保证 commit 后返回的
   detached ORM 对象属性仍可读；
3) Session 生命周期：每个方法内部 `with self._session_factory() as session:`
   管理，方法结束自动 close；
4) 不吞异常：SQLAlchemy 原生异常包装为 PluginOperationError 后抛出，
   保留 `raise ... from e` 异常链；
5) 查询方法（get_by_plugin_id / get_by_plugin_name_norm / get_by_secret_hash /
   get_by_id）查不到返回 None；update / clear / delete 类写操作不存在抛
   PluginNotFoundError。

安全红线（Phase 3.5 §8，严格执行）：
- 本模块只接触 plugin_secret_hash / api_key_ciphertext / api_key_nonce，
  绝不接触 plugin_secret / API Key 明文；
- 错误消息绝不包含 plugin_secret / secret_hash 完整值 / api_key_ciphertext /
  api_key_nonce / API Key 明文 / SQLAlchemy [parameters: ...]；
- plugin_id 属定位信息（非秘密）可出现在错误消息，但统一用 _hash_prefix
  截断保守处理；
- 数据库异常只输出简短非敏感诊断（_db_error_brief）；
- _hash_prefix / _db_error_brief 在本模块内独立定义。

字段修改边界（对齐 Protocol）：
- create_plugin      ：只写 plugin_id / plugin_name / plugin_name_norm /
                       plugin_secret_hash / api_key_ciphertext / api_key_nonce / status；
- update_plugin_name ：只动 plugin_name / plugin_name_norm；
- update_api_key     ：只动 api_key_ciphertext / api_key_nonce；
- clear_api_key      ：只置 api_key_ciphertext / api_key_nonce = NULL；
- update_status      ：只动 status（前置校验 PluginStatus.ALL）；
- delete_plugin      ：删除整行（本阶段只删 MySQL 行）；
- 禁止修改 id / plugin_id / plugin_secret_hash。
"""

from __future__ import annotations

from sqlalchemy import Engine, select
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from ...core.exceptions import (
    PluginNotFoundError,
    PluginOperationError,
)
from ...models.plugin import PluginStatus, PluginWorkspace
from .plugin_protocol import PluginRepository


def _hash_prefix(value: str | None) -> str:
    """日志用前缀（前 8 位 + 长度），避免在错误信息中出现完整值。"""
    if not value:
        return "<empty>"
    return f"{value[:8]}... (len={len(value)})"


def _db_error_brief(e: BaseException) -> str:
    """从 SQLAlchemy 异常提取单行简短诊断，避免把完整 SQL 与绑定参数
    （含 plugin_secret_hash / ciphertext 值）写入错误消息。

    SQLAlchemy 的 str(exception) 含 `[SQL: ...]` 与 `[parameters: ...]`，
    其中 parameters 可能包含完整 hash / ciphertext——严禁外泄。
    取 e.orig（驱动层原生异常，如 'UNIQUE constraint failed: plugin_workspaces.plugin_id'）
    或 str(e) 的首行作为安全摘要。
    """
    brief = str(getattr(e, "orig", None) or e)
    return brief.splitlines()[0]


class PluginRepositoryImpl(PluginRepository):
    """PluginRepository 的 SQLAlchemy 实现（MySQL plugin_workspaces 表 CRUD）。"""

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
        插入一条 Plugin Workspace 记录，返回 detached ORM 对象。

        不生成 plugin_id / secret / hash / 加密：所有安全计算由上层（Security +
        PluginService）完成，本方法只负责 INSERT plugin_workspaces。
        api_key_ciphertext / api_key_nonce 缺省或 None 时保持 NULL（未配置模型 Key）。
        """
        plugin = PluginWorkspace(
            plugin_id=plugin_id,
            plugin_name=plugin_name,
            plugin_name_norm=plugin_name_norm,
            plugin_secret_hash=plugin_secret_hash,
            status=status,
        )
        if api_key_ciphertext is not None:
            plugin.api_key_ciphertext = api_key_ciphertext
        if api_key_nonce is not None:
            plugin.api_key_nonce = api_key_nonce
        try:
            with self._session_factory() as session:
                session.add(plugin)
                session.commit()
                session.refresh(plugin)
                return plugin
        except (OperationalError, IntegrityError, DBAPIError) as e:
            raise PluginOperationError(
                f"create_plugin failed: "
                f"plugin_id={_hash_prefix(plugin_id)}, "
                f"plugin_name={plugin_name!r}, "
                f"error={_db_error_brief(e)}"
            ) from e

    # ---------------------------------------------------------- get by plugin_id
    def get_by_plugin_id(self, plugin_id: str) -> PluginWorkspace | None:
        """按 plugin_id 精确查询；查不到返回 None（不抛 auth 异常）。"""
        try:
            with self._session_factory() as session:
                return (
                    session.execute(
                        select(PluginWorkspace).where(
                            PluginWorkspace.plugin_id == plugin_id
                        )
                    )
                    .scalars()
                    .first()
                )
        except (OperationalError, DBAPIError) as e:
            raise PluginOperationError(
                f"get_by_plugin_id failed: "
                f"plugin_id={_hash_prefix(plugin_id)}, "
                f"error={_db_error_brief(e)}"
            ) from e

    # ------------------------------------------------------- get by name norm
    def get_by_plugin_name_norm(self, plugin_name_norm: str) -> PluginWorkspace | None:
        """按归一化名称精确查询；查不到返回 None。"""
        try:
            with self._session_factory() as session:
                return (
                    session.execute(
                        select(PluginWorkspace).where(
                            PluginWorkspace.plugin_name_norm == plugin_name_norm
                        )
                    )
                    .scalars()
                    .first()
                )
        except (OperationalError, DBAPIError) as e:
            raise PluginOperationError(
                f"get_by_plugin_name_norm failed: "
                f"plugin_name_norm={plugin_name_norm!r}, "
                f"error={_db_error_brief(e)}"
            ) from e

    # ------------------------------------------------------- get by secret hash
    def get_by_secret_hash(self, plugin_secret_hash: str) -> PluginWorkspace | None:
        """按 secret 哈希精确查询；查不到返回 None（不抛 auth 异常）。"""
        try:
            with self._session_factory() as session:
                return (
                    session.execute(
                        select(PluginWorkspace).where(
                            PluginWorkspace.plugin_secret_hash == plugin_secret_hash
                        )
                    )
                    .scalars()
                    .first()
                )
        except (OperationalError, DBAPIError) as e:
            raise PluginOperationError(
                f"get_by_secret_hash failed: error={_db_error_brief(e)}"
            ) from e

    # ----------------------------------------------------------------- get by id
    def get_by_id(self, plugin_workspace_id: int) -> PluginWorkspace | None:
        """按主键查询；查不到返回 None。"""
        try:
            with self._session_factory() as session:
                return session.get(PluginWorkspace, plugin_workspace_id)
        except (OperationalError, DBAPIError) as e:
            raise PluginOperationError(
                f"get_by_id failed: id={plugin_workspace_id}, "
                f"error={_db_error_brief(e)}"
            ) from e

    # --------------------------------------------------------- update_plugin_name
    def update_plugin_name(
        self,
        plugin_id: str,
        plugin_name: str,
        plugin_name_norm: str,
    ) -> PluginWorkspace:
        """
        更新展示名与归一化名：只更新 plugin_name / plugin_name_norm
        （updated_at 由 ORM onupdate=func.now() 自动刷新）。

        不改变 id / plugin_id / plugin_secret_hash / api_key_* / status。
        """
        try:
            with self._session_factory() as session:
                plugin = self._get_by_plugin_id_in_session(session, plugin_id)
                plugin.plugin_name = plugin_name
                plugin.plugin_name_norm = plugin_name_norm
                session.commit()
                session.refresh(plugin)
                return plugin
        except PluginNotFoundError:
            raise
        except (OperationalError, IntegrityError, DBAPIError) as e:
            raise PluginOperationError(
                f"update_plugin_name failed: "
                f"plugin_id={_hash_prefix(plugin_id)}, "
                f"error={_db_error_brief(e)}"
            ) from e

    # ------------------------------------------------------------- update_api_key
    def update_api_key(
        self,
        plugin_id: str,
        api_key_ciphertext: str,
        api_key_nonce: str,
    ) -> PluginWorkspace:
        """
        更换模型 API Key：只更新 api_key_ciphertext / api_key_nonce
        （updated_at 由 ORM onupdate=func.now() 自动刷新）。

        不改变 id / plugin_id / plugin_name / plugin_name_norm /
        plugin_secret_hash / status。
        """
        try:
            with self._session_factory() as session:
                plugin = self._get_by_plugin_id_in_session(session, plugin_id)
                plugin.api_key_ciphertext = api_key_ciphertext
                plugin.api_key_nonce = api_key_nonce
                session.commit()
                session.refresh(plugin)
                return plugin
        except PluginNotFoundError:
            raise
        except (OperationalError, IntegrityError, DBAPIError) as e:
            raise PluginOperationError(
                f"update_api_key failed: "
                f"plugin_id={_hash_prefix(plugin_id)}, "
                f"error={_db_error_brief(e)}"
            ) from e

    # --------------------------------------------------------------- clear_api_key
    def clear_api_key(self, plugin_id: str) -> PluginWorkspace:
        """
        清除模型 API Key：只置 api_key_ciphertext / api_key_nonce = NULL
        （updated_at 由 ORM onupdate=func.now() 自动刷新）。

        不改变 id / plugin_id / plugin_name / plugin_name_norm /
        plugin_secret_hash / status，不影响 documents 归属。
        """
        try:
            with self._session_factory() as session:
                plugin = self._get_by_plugin_id_in_session(session, plugin_id)
                plugin.api_key_ciphertext = None
                plugin.api_key_nonce = None
                session.commit()
                session.refresh(plugin)
                return plugin
        except PluginNotFoundError:
            raise
        except (OperationalError, IntegrityError, DBAPIError) as e:
            raise PluginOperationError(
                f"clear_api_key failed: "
                f"plugin_id={_hash_prefix(plugin_id)}, "
                f"error={_db_error_brief(e)}"
            ) from e

    # --------------------------------------------------------------- update_status
    def update_status(self, plugin_id: str, status: str) -> PluginWorkspace:
        """
        更新 Workspace 状态：只更新 status（updated_at 由 ORM onupdate 自动刷新）。

        前置校验 status ∈ PluginStatus.ALL；非法值抛 PluginOperationError。
        不改变 id / plugin_id / plugin_name / plugin_name_norm /
        plugin_secret_hash / api_key_*。
        """
        if status not in PluginStatus.ALL:
            raise PluginOperationError(
                f"update_status failed: invalid status={status!r}, "
                f"allowed={sorted(PluginStatus.ALL)}"
            )
        try:
            with self._session_factory() as session:
                plugin = self._get_by_plugin_id_in_session(session, plugin_id)
                plugin.status = status
                session.commit()
                session.refresh(plugin)
                return plugin
        except PluginNotFoundError:
            raise
        except (OperationalError, IntegrityError, DBAPIError) as e:
            raise PluginOperationError(
                f"update_status failed: "
                f"plugin_id={_hash_prefix(plugin_id)}, "
                f"status={status!r}, "
                f"error={_db_error_brief(e)}"
            ) from e

    # ------------------------------------------------------------------ delete
    def delete_plugin(self, plugin_id: str) -> PluginWorkspace:
        """
        删除一个 Plugin Workspace 行（本阶段只删 MySQL 行；跨系统清理编排属
        PluginService 职责）。返回被删对象（detached 可读）。
        """
        try:
            with self._session_factory() as session:
                plugin = self._get_by_plugin_id_in_session(session, plugin_id)
                session.delete(plugin)
                session.commit()
                return plugin
        except PluginNotFoundError:
            raise
        except (OperationalError, IntegrityError, DBAPIError) as e:
            raise PluginOperationError(
                f"delete_plugin failed: "
                f"plugin_id={_hash_prefix(plugin_id)}, "
                f"error={_db_error_brief(e)}"
            ) from e

    # ------------------------------------------------------- session 内按 id 查询
    @staticmethod
    def _get_by_plugin_id_in_session(
        session: Session, plugin_id: str
    ) -> PluginWorkspace:
        """session 内按 plugin_id 查询；不存在抛 PluginNotFoundError。"""
        plugin = (
            session.execute(
                select(PluginWorkspace).where(
                    PluginWorkspace.plugin_id == plugin_id
                )
            )
            .scalars()
            .first()
        )
        if plugin is None:
            raise PluginNotFoundError(f"plugin not found: plugin_id={plugin_id}")
        return plugin
