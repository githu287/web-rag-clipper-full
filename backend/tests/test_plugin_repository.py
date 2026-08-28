"""
PluginRepositoryImpl 单元测试（Phase 3.5 Step 2-B 新增：MySQL plugin_workspaces 表 CRUD）。

测试策略（与 test_user_repository.py / test_document_repository.py 完全对齐）：
1) 用 SQLite in-memory engine + StaticPool 替换真实 MySQL，避免外部依赖；
2) setUpClass 建 engine + create_all；setUp 清空 plugin_workspaces 表保证方法间隔离；
3) 覆盖：创建 / 查询 / 不存在 / 改名 / API Key / 状态 / 唯一约束 / 删除；
4) 验证 Protocol runtime_checkable：Impl 实例 isinstance PluginRepository。

安全红线验证（Phase 3.5 §8）：
- 唯一约束冲突抛 PluginOperationError 时，错误消息不包含完整 plugin_id /
  plugin_secret_hash / api_key_ciphertext / api_key_nonce / SQLAlchemy
  [parameters: ...]（Impl 已用 _hash_prefix + _db_error_brief 处理）。

Repository 职责边界验证（Phase 3.5 §7）：
- create_plugin 接收的 plugin_id / plugin_name / plugin_name_norm /
  plugin_secret_hash / ciphertext / nonce 均为已计算好的值，Repository 不加工、
  不生成；本测试只验证持久化正确性，不涉及生成 / 哈希 / 加密逻辑。

不依赖：
- 真实 MySQL（SQLite in-memory 替代）
- 真实 .env（无 Settings 依赖，直接构造 engine）
- FastAPI / Service / API / PluginService（尚未创建）
"""

from __future__ import annotations

import unittest

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import StaticPool

from backend.core.exceptions import (
    PluginNotFoundError,
    PluginOperationError,
)
from backend.models.base import Base
from backend.models.plugin import PluginStatus, PluginWorkspace
from backend.repositories.mysql import (
    PluginRepository,
    PluginRepositoryImpl,
)


def _make_test_engine() -> Engine:
    """构造 SQLite in-memory engine（StaticPool 保证多连接共享同一内存库）。"""
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


class PluginRepositoryTest(unittest.TestCase):
    """PluginRepositoryImpl CRUD 单元测试（plugin_workspaces 表）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine: Engine = _make_test_engine()
        Base.metadata.create_all(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        Base.metadata.drop_all(cls.engine)
        cls.engine.dispose()

    def setUp(self) -> None:
        """每个测试方法清空 plugin_workspaces，保证数据隔离。"""
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM plugin_workspaces"))
        self.repo: PluginRepositoryImpl = PluginRepositoryImpl(self.engine)

    # --------------------------------------------------------------- 工具方法
    @staticmethod
    def _make_create_kwargs(
        plugin_id: str = "plugin-4f0d1c2b3a99887766554433221100ffeeddccbbaa",
        plugin_name: str = "测试插件",
        plugin_name_norm: str = "测试插件",
        plugin_secret_hash: str = "a" * 64,
    ) -> dict:
        return {
            "plugin_id": plugin_id,
            "plugin_name": plugin_name,
            "plugin_name_norm": plugin_name_norm,
            "plugin_secret_hash": plugin_secret_hash,
        }

    # ------------------------------------------------------------------ 创建
    def test_create_plugin_all_fields(self) -> None:
        """创建：全部字段落库，plugin_id / name / norm / secret_hash 正确。"""
        plugin = self.repo.create_plugin(
            **self._make_create_kwargs(),
            api_key_ciphertext="cipher-b64",
            api_key_nonce="nonce-b64",
        )

        self.assertIsInstance(plugin, PluginWorkspace)
        self.assertIsNotNone(plugin.id)
        self.assertGreater(plugin.id, 0)
        self.assertEqual(plugin.plugin_id, "plugin-4f0d1c2b3a99887766554433221100ffeeddccbbaa")
        self.assertEqual(plugin.plugin_name, "测试插件")
        self.assertEqual(plugin.plugin_name_norm, "测试插件")
        self.assertEqual(plugin.plugin_secret_hash, "a" * 64)
        self.assertEqual(plugin.api_key_ciphertext, "cipher-b64")
        self.assertEqual(plugin.api_key_nonce, "nonce-b64")
        self.assertEqual(plugin.status, PluginStatus.ACTIVE)
        self.assertIsNotNone(plugin.created_at)
        self.assertIsNotNone(plugin.updated_at)

    def test_create_plugin_api_key_optional(self) -> None:
        """创建：不传 api_key → ciphertext / nonce 保持 NULL（未配置模型 Key）。"""
        plugin = self.repo.create_plugin(**self._make_create_kwargs())

        self.assertIsNone(plugin.api_key_ciphertext)
        self.assertIsNone(plugin.api_key_nonce)
        self.assertEqual(plugin.status, PluginStatus.ACTIVE)

    def test_create_plugin_custom_status(self) -> None:
        """创建：显式传入非默认 status 落库。"""
        plugin = self.repo.create_plugin(
            **self._make_create_kwargs(), status=PluginStatus.DISABLED
        )
        self.assertEqual(plugin.status, PluginStatus.DISABLED)

    # ------------------------------------------------------------------ 查询
    def test_get_by_plugin_id(self) -> None:
        """查询：按 plugin_id 精确命中。"""
        created = self.repo.create_plugin(**self._make_create_kwargs())
        fetched = self.repo.get_by_plugin_id(created.plugin_id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.id, created.id)
        self.assertEqual(fetched.plugin_name, "测试插件")
        self.assertEqual(fetched.plugin_secret_hash, "a" * 64)

    def test_get_by_plugin_name_norm(self) -> None:
        """查询：按归一化名称精确命中。"""
        created = self.repo.create_plugin(
            **self._make_create_kwargs(plugin_name="My Plugin", plugin_name_norm="my plugin")
        )
        fetched = self.repo.get_by_plugin_name_norm("my plugin")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.plugin_id, created.plugin_id)

    def test_get_by_secret_hash(self) -> None:
        """查询：按 secret 哈希精确命中。"""
        created = self.repo.create_plugin(
            **self._make_create_kwargs(plugin_secret_hash="b" * 64)
        )
        fetched = self.repo.get_by_secret_hash("b" * 64)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.plugin_id, created.plugin_id)

    def test_get_by_id(self) -> None:
        """查询：按主键精确命中。"""
        created = self.repo.create_plugin(**self._make_create_kwargs())
        fetched = self.repo.get_by_id(created.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.plugin_id, created.plugin_id)

    # ---------------------------------------------------------- 不存在 → None
    def test_get_by_plugin_id_missing_returns_none(self) -> None:
        """不存在：get_by_plugin_id 返回 None（不抛异常）。"""
        self.assertIsNone(self.repo.get_by_plugin_id("no-such-plugin-id"))

    def test_get_by_plugin_name_norm_missing_returns_none(self) -> None:
        """不存在：get_by_plugin_name_norm 返回 None（不抛异常）。"""
        self.assertIsNone(self.repo.get_by_plugin_name_norm("no-such-name"))

    def test_get_by_secret_hash_missing_returns_none(self) -> None:
        """不存在：get_by_secret_hash 返回 None（不抛异常）。"""
        self.assertIsNone(self.repo.get_by_secret_hash("f" * 64))

    def test_get_by_id_missing_returns_none(self) -> None:
        """不存在：get_by_id 返回 None（不抛异常）。"""
        self.assertIsNone(self.repo.get_by_id(99999))

    # -------------------------------------------------------------- Name 更新
    def test_update_plugin_name_keeps_identity_and_keys(self) -> None:
        """改名：plugin_id / secret_hash / API Key 不变，name / norm 更新。"""
        created = self.repo.create_plugin(
            **self._make_create_kwargs(),
            api_key_ciphertext="cipher-1",
            api_key_nonce="nonce-1",
        )

        updated = self.repo.update_plugin_name(
            created.plugin_id, "新名字", "新名字"
        )

        # 身份不变
        self.assertEqual(updated.plugin_id, created.plugin_id)
        self.assertEqual(updated.plugin_secret_hash, created.plugin_secret_hash)
        # name / norm 更新
        self.assertEqual(updated.plugin_name, "新名字")
        self.assertEqual(updated.plugin_name_norm, "新名字")
        # API Key 不变
        self.assertEqual(updated.api_key_ciphertext, "cipher-1")
        self.assertEqual(updated.api_key_nonce, "nonce-1")

        # DB 持久化二次验证
        refetched = self.repo.get_by_plugin_id(created.plugin_id)
        self.assertEqual(refetched.plugin_name, "新名字")
        self.assertEqual(refetched.plugin_name_norm, "新名字")

    def test_update_plugin_name_not_found(self) -> None:
        """改名：plugin_id 不存在抛 PluginNotFoundError。"""
        with self.assertRaises(PluginNotFoundError):
            self.repo.update_plugin_name("missing-plugin-id", "x", "x")

    # ---------------------------------------------------------------- API Key
    def test_update_api_key(self) -> None:
        """换 Key：ciphertext / nonce 更新，plugin_id / secret_hash 不变。"""
        created = self.repo.create_plugin(**self._make_create_kwargs())
        self.assertIsNone(created.api_key_ciphertext)

        updated = self.repo.update_api_key(created.plugin_id, "cipher-new", "nonce-new")

        self.assertEqual(updated.plugin_id, created.plugin_id)
        self.assertEqual(updated.plugin_secret_hash, created.plugin_secret_hash)
        self.assertEqual(updated.api_key_ciphertext, "cipher-new")
        self.assertEqual(updated.api_key_nonce, "nonce-new")

    def test_clear_api_key(self) -> None:
        """清 Key：ciphertext / nonce 置 NULL，plugin_id / secret_hash 不变。"""
        created = self.repo.create_plugin(
            **self._make_create_kwargs(),
            api_key_ciphertext="cipher-1",
            api_key_nonce="nonce-1",
        )

        cleared = self.repo.clear_api_key(created.plugin_id)

        self.assertEqual(cleared.plugin_id, created.plugin_id)
        self.assertEqual(cleared.plugin_secret_hash, created.plugin_secret_hash)
        self.assertIsNone(cleared.api_key_ciphertext)
        self.assertIsNone(cleared.api_key_nonce)

        # DB 持久化二次验证
        refetched = self.repo.get_by_plugin_id(created.plugin_id)
        self.assertIsNone(refetched.api_key_ciphertext)
        self.assertIsNone(refetched.api_key_nonce)

    def test_update_api_key_not_found(self) -> None:
        """换 Key：plugin_id 不存在抛 PluginNotFoundError。"""
        with self.assertRaises(PluginNotFoundError):
            self.repo.update_api_key("missing-plugin-id", "c", "n")

    def test_clear_api_key_not_found(self) -> None:
        """清 Key：plugin_id 不存在抛 PluginNotFoundError。"""
        with self.assertRaises(PluginNotFoundError):
            self.repo.clear_api_key("missing-plugin-id")

    # ------------------------------------------------------------------ 状态
    def test_update_status_round_trip(self) -> None:
        """状态：ACTIVE → DISABLED → ACTIVE 往返，其余字段不变。"""
        created = self.repo.create_plugin(**self._make_create_kwargs())
        self.assertEqual(created.status, PluginStatus.ACTIVE)

        disabled = self.repo.update_status(created.plugin_id, PluginStatus.DISABLED)
        self.assertEqual(disabled.status, PluginStatus.DISABLED)
        self.assertEqual(disabled.plugin_id, created.plugin_id)
        self.assertEqual(disabled.plugin_secret_hash, created.plugin_secret_hash)

        active = self.repo.update_status(created.plugin_id, PluginStatus.ACTIVE)
        self.assertEqual(active.status, PluginStatus.ACTIVE)

    def test_update_status_invalid_raises(self) -> None:
        """状态：非法 status 抛 PluginOperationError，DB 不变。"""
        created = self.repo.create_plugin(**self._make_create_kwargs())

        with self.assertRaises(PluginOperationError):
            self.repo.update_status(created.plugin_id, "BOGUS")

        refetched = self.repo.get_by_plugin_id(created.plugin_id)
        self.assertEqual(refetched.status, PluginStatus.ACTIVE)

    def test_update_status_not_found(self) -> None:
        """状态：plugin_id 不存在抛 PluginNotFoundError。"""
        with self.assertRaises(PluginNotFoundError):
            self.repo.update_status("missing-plugin-id", PluginStatus.DISABLED)

    # ---------------------------------------------------------------- 唯一约束
    def test_duplicate_plugin_id_rejected(self) -> None:
        """唯一约束：重复 plugin_id → PluginOperationError，不泄漏完整参数。"""
        self.repo.create_plugin(**self._make_create_kwargs())

        with self.assertRaises(PluginOperationError) as cm:
            self.repo.create_plugin(
                plugin_id="plugin-4f0d1c2b3a99887766554433221100ffeeddccbbaa",
                plugin_name="另一个",
                plugin_name_norm="another",
                plugin_secret_hash="c" * 64,
            )

        message = str(cm.exception)
        # 安全红线：不包含完整 plugin_id / secret_hash / SQL 参数
        self.assertNotIn("4f0d1c2b3a99887766554433221100ffeeddccbbaa", message)
        self.assertNotIn("c" * 64, message)
        self.assertNotIn("[parameters:", message)

    def test_duplicate_plugin_name_norm_rejected(self) -> None:
        """唯一约束：重复 plugin_name_norm → PluginOperationError。"""
        self.repo.create_plugin(**self._make_create_kwargs())

        with self.assertRaises(PluginOperationError) as cm:
            self.repo.create_plugin(
                plugin_id="plugin-2f0d1c2b3a99887766554433221100ffeeddccbbaa",
                plugin_name="Another",
                plugin_name_norm="测试插件",  # 与已存在的归一化名相同
                plugin_secret_hash="d" * 64,
            )

        message = str(cm.exception)
        self.assertNotIn("d" * 64, message)
        self.assertNotIn("[parameters:", message)

    def test_duplicate_plugin_secret_hash_rejected(self) -> None:
        """唯一约束：重复 plugin_secret_hash → PluginOperationError。"""
        self.repo.create_plugin(**self._make_create_kwargs())

        with self.assertRaises(PluginOperationError) as cm:
            self.repo.create_plugin(
                plugin_id="plugin-3f0d1c2b3a99887766554433221100ffeeddccbbaa",
                plugin_name="Another",
                plugin_name_norm="another",
                plugin_secret_hash="a" * 64,  # 与已存在的 hash 相同
            )

        message = str(cm.exception)
        self.assertNotIn("a" * 64, message)
        self.assertNotIn("[parameters:", message)

    # ------------------------------------------------------------------ 删除
    def test_delete_plugin(self) -> None:
        """删除：返回被删对象，之后查询返回 None。"""
        created = self.repo.create_plugin(**self._make_create_kwargs())

        deleted = self.repo.delete_plugin(created.plugin_id)

        self.assertEqual(deleted.plugin_id, created.plugin_id)
        self.assertIsNone(self.repo.get_by_plugin_id(created.plugin_id))

    def test_delete_plugin_not_found(self) -> None:
        """删除：plugin_id 不存在抛 PluginNotFoundError。"""
        with self.assertRaises(PluginNotFoundError):
            self.repo.delete_plugin("missing-plugin-id")

    # ------------------------------------------------------- protocol conformance
    def test_protocol_runtime_checkable(self) -> None:
        """验证 PluginRepositoryImpl 实例 isinstance PluginRepository。"""
        self.assertIsInstance(self.repo, PluginRepository)


if __name__ == "__main__":
    unittest.main()
