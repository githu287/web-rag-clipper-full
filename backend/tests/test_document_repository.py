"""
DocumentRepositoryImpl 单元测试（Phase 2.9 Step 1；Phase 3.1 Step 3 扩展
title / url / source_type 来源元数据保存；Phase 3.4 Step C user-aware 升级；
Phase 3.5 Step 2-B plugin-aware 切换）。

测试策略：
1) 用 SQLite in-memory engine + StaticPool 替换真实 MySQL，避免外部依赖；
2) 每个测试方法 setUp 重建 engine + create_all，保证方法间数据隔离；
3) 覆盖 CRUD 方法的正常路径 + 异常路径 + 边界条件；
4) 验证 expire_on_commit=False：commit 后 detached ORM 属性可读；
5) 验证 Protocol runtime_checkable：Impl 实例 isinstance DocumentRepository。

Phase 3.1 Step 3 新增覆盖：
- create_document 显式传 title/url/source_type 正确保存；
- 旧调用（不传 3 个新参数）保持兼容：title/url=NULL、source_type 默认 upload；
- title=None 显式传与缺省等价，url 独立落库。

Phase 3.4 Step C 新增覆盖（user-aware，历史）：
- create_document：user_id 必填；get/delete 双条件 + 跨用户隔离。

Phase 3.5 Step 2-B 覆盖（plugin-aware，取代 Step C）：
- create_document：plugin_id 必填（不传 → TypeError；None → NOT NULL 冲突）；
- 预置两个 Plugin Workspace（PLUGIN_A / PLUGIN_B），documents.plugin_id
  FK → plugin_workspaces.plugin_id（SQLite 默认不强制 FK，但保持真实 MySQL 语义）；
- get_document(document_id, plugin_id)：A 只能读 A；A 读 B → DocumentNotFoundError；
- get_documents_by_ids(document_ids, plugin_id)：SQL 层过滤只返回 A；空 ids → []；
- get_success_document_ids(plugin_id)：只返回 A 的 SUCCESS id，不含 A 的非 SUCCESS
  与 B 的 SUCCESS；
- delete_document(document_id, plugin_id)：A 删 B → DocumentNotFoundError，B 的文档仍在；
- update_status / update_ingest_result / update_failure 不受 plugin-aware 影响。

不依赖：
- 真实 MySQL（SQLite in-memory 替代）
- 真实 .env（无 Settings 依赖，直接构造 engine）
- FastAPI / Service / API
"""

from __future__ import annotations

import unittest
from datetime import datetime

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import StaticPool

from backend.core.exceptions import (
    DocumentNotFoundError,
    DocumentOperationError,
)
from backend.models.base import Base
from backend.models.document import Document, DocumentStatus
from backend.repositories.mysql import (
    DocumentRepository,
    DocumentRepositoryImpl,
)


def _make_test_engine() -> Engine:
    """
    构造 SQLite in-memory engine（StaticPool 保证多连接共享同一内存库）。

    StaticPool：所有 connection 复用同一 in-memory DB connection，保证
    create_all 建的表与 Repository session 使用的 session 看到同一份数据。
    check_same_thread=False：允许跨线程使用（unittest 某些场景需要）。
    """
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


# Phase 3.5 Step 2-B：两个隔离 Plugin Workspace 标识（模拟 plugin_id，VARCHAR(64)）
PLUGIN_A = "plugin-a-4f0d1c2b3a99887766554433221100ffeeddccbbaa"
PLUGIN_B = "plugin-b-00aa11bb22cc33dd44ee55ff66778899aabbccdd"


def _seed_plugin_workspaces(engine: Engine) -> None:
    """预置两个 Plugin Workspace（documents.plugin_id FK 目标，保持真实 MySQL 语义）。"""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO plugin_workspaces"
                " (plugin_id, plugin_name, plugin_name_norm, plugin_secret_hash, status)"
                " VALUES (:pid, :pname, :pnorm, :shash, 'ACTIVE')"
            ),
            [
                {
                    "pid": PLUGIN_A,
                    "pname": "Plugin A",
                    "pnorm": "plugin a",
                    "shash": "a" * 64,
                },
                {
                    "pid": PLUGIN_B,
                    "pname": "Plugin B",
                    "pnorm": "plugin b",
                    "shash": "b" * 64,
                },
            ],
        )


class DocumentRepositoryImplTest(unittest.TestCase):
    """DocumentRepositoryImpl CRUD 单元测试（plugin-aware）。"""

    def setUp(self) -> None:
        """每个测试方法重建 engine + create_all，保证数据隔离。"""
        self.engine: Engine = _make_test_engine()
        Base.metadata.create_all(self.engine)
        _seed_plugin_workspaces(self.engine)
        self.repo: DocumentRepositoryImpl = DocumentRepositoryImpl(self.engine)

    def tearDown(self) -> None:
        """测试结束销毁表，释放 in-memory DB。"""
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    # ------------------------------------------------------------------ create
    def test_create_document_defaults(self) -> None:
        """create_document：默认 status=PENDING、chunk_count=0、plugin_id 必填落库。"""
        doc = self.repo.create_document(
            filename="test.pdf",
            file_path="/data/test.pdf",
            plugin_id=PLUGIN_A,
        )

        self.assertIsNotNone(doc.id)
        self.assertGreater(doc.id, 0)
        self.assertEqual(doc.filename, "test.pdf")
        self.assertEqual(doc.file_path, "/data/test.pdf")
        self.assertEqual(doc.plugin_id, PLUGIN_A)
        self.assertEqual(doc.status, DocumentStatus.PENDING)
        self.assertEqual(doc.chunk_count, 0)
        self.assertIsInstance(doc.created_at, datetime)
        self.assertIsInstance(doc.updated_at, datetime)
        # 不严格断言 updated_at > created_at（SQLite 时间精度可能相同）
        self.assertGreaterEqual(doc.updated_at, doc.created_at)

    def test_create_document_with_plugin_id(self) -> None:
        """create_document：显式传入 plugin_id（Workspace 归属落库）。"""
        doc = self.repo.create_document(
            filename="plugin_doc.pdf",
            file_path="/data/plugin_doc.pdf",
            plugin_id=PLUGIN_B,
        )

        self.assertEqual(doc.plugin_id, PLUGIN_B)
        self.assertEqual(doc.filename, "plugin_doc.pdf")

    def test_create_document_with_file_metadata(self) -> None:
        """create_document：显式传入 file_size / mime_type（Phase 2.10 Step 3）。"""
        doc = self.repo.create_document(
            filename="meta.txt",
            file_path="/data/meta.txt",
            plugin_id=PLUGIN_B,
            file_size=1024,
            mime_type="text/plain",
        )

        self.assertEqual(doc.file_size, 1024)
        self.assertEqual(doc.mime_type, "text/plain")
        self.assertEqual(doc.status, DocumentStatus.PENDING)
        self.assertIsNone(doc.error_message)

    def test_create_document_default_file_metadata(self) -> None:
        """create_document：不传 file_size / mime_type 时保持默认 0 / ""。"""
        doc = self.repo.create_document(
            filename="no_meta.txt",
            file_path="/data/no_meta.txt",
            plugin_id=PLUGIN_B,
        )

        self.assertEqual(doc.file_size, 0)
        self.assertEqual(doc.mime_type, "")

    # ------------------------------------------- Phase 3.1 Step 3：来源元数据
    def test_create_document_with_source_metadata(self) -> None:
        """create_document：显式传入 title / url / source_type（网页剪藏）。"""
        doc = self.repo.create_document(
            filename="webclip.txt",
            file_path="",
            plugin_id=PLUGIN_A,
            title="示例文章",
            url="https://example.com/article/1",
            source_type="webpage",
        )

        self.assertEqual(doc.filename, "webclip.txt")
        self.assertEqual(doc.file_path, "")
        self.assertEqual(doc.title, "示例文章")
        self.assertEqual(doc.url, "https://example.com/article/1")
        self.assertEqual(doc.source_type, "webpage")

        # 二次查询验证 DB 持久化
        refetched = self.repo.get_document(doc.id, plugin_id=PLUGIN_A)
        self.assertEqual(refetched.title, "示例文章")
        self.assertEqual(refetched.url, "https://example.com/article/1")
        self.assertEqual(refetched.source_type, "webpage")

    def test_create_document_default_source_metadata(self) -> None:
        """create_document：不传 title/url/source_type → NULL / 默认 upload（旧调用兼容）。"""
        doc = self.repo.create_document(
            filename="legacy.txt",
            file_path="/data/legacy.txt",
            plugin_id=PLUGIN_A,
        )

        self.assertIsNone(doc.title)
        self.assertIsNone(doc.url)
        self.assertEqual(doc.source_type, "upload")

        refetched = self.repo.get_document(doc.id, plugin_id=PLUGIN_A)
        self.assertIsNone(refetched.title)
        self.assertIsNone(refetched.url)
        self.assertEqual(refetched.source_type, "upload")

    def test_create_document_title_none_url_kept(self) -> None:
        """create_document：title=None 显式传与缺省等价；url 独立落库。"""
        doc = self.repo.create_document(
            filename="webclip2.txt",
            file_path="",
            plugin_id=PLUGIN_A,
            title=None,
            url="https://example.com/2",
            source_type="webpage",
        )

        self.assertIsNone(doc.title)
        self.assertEqual(doc.url, "https://example.com/2")
        self.assertEqual(doc.source_type, "webpage")

    # -------------------------------------------------------------------- get
    def test_get_document_success(self) -> None:
        """get_document：正常查询返回 ORM 对象。"""
        created = self.repo.create_document(
            filename="get.pdf",
            file_path="/data/get.pdf",
            plugin_id=PLUGIN_A,
        )

        fetched = self.repo.get_document(created.id, plugin_id=PLUGIN_A)

        self.assertEqual(fetched.id, created.id)
        self.assertEqual(fetched.filename, "get.pdf")
        self.assertEqual(fetched.file_path, "/data/get.pdf")
        self.assertEqual(fetched.plugin_id, PLUGIN_A)
        self.assertEqual(fetched.status, DocumentStatus.PENDING)
        self.assertEqual(fetched.chunk_count, 0)

    def test_get_document_not_found(self) -> None:
        """get_document：不存在的主键抛 DocumentNotFoundError。"""
        with self.assertRaises(DocumentNotFoundError):
            self.repo.get_document(99999, plugin_id=PLUGIN_A)

    # ----------------------------------------------------------- update_status
    def test_update_status_all_states(self) -> None:
        """update_status：遍历 DocumentStatus.ALL 所有合法状态。"""
        doc = self.repo.create_document(
            filename="status.pdf",
            file_path="/data/status.pdf",
            plugin_id=PLUGIN_A,
        )

        for status in DocumentStatus.ALL:
            updated = self.repo.update_status(doc.id, status)
            self.assertEqual(updated.status, status)

            # 二次查询验证 DB 持久化
            refetched = self.repo.get_document(doc.id, plugin_id=PLUGIN_A)
            self.assertEqual(refetched.status, status)

    def test_update_status_invalid(self) -> None:
        """update_status：非法 status 抛 DocumentOperationError，DB 不变。"""
        doc = self.repo.create_document(
            filename="invalid.pdf",
            file_path="/data/invalid.pdf",
            plugin_id=PLUGIN_A,
        )

        with self.assertRaises(DocumentOperationError):
            self.repo.update_status(doc.id, "INVALID_STATUS")

        # 验证 DB 仍为默认 PENDING（未发 SQL）
        refetched = self.repo.get_document(doc.id, plugin_id=PLUGIN_A)
        self.assertEqual(refetched.status, DocumentStatus.PENDING)

    def test_update_status_not_found(self) -> None:
        """update_status：不存在的主键抛 DocumentNotFoundError。"""
        with self.assertRaises(DocumentNotFoundError):
            self.repo.update_status(88888, DocumentStatus.SUCCESS)

    # ------------------------------------------------------ update_ingest_result
    def test_update_ingest_result_success(self) -> None:
        """update_ingest_result：单次调用同时更新 chunk_count + status。"""
        doc = self.repo.create_document(
            filename="ingest_result.pdf",
            file_path="/data/ingest_result.pdf",
            plugin_id=PLUGIN_A,
        )
        # 前置状态：默认 PENDING / chunk_count=0
        self.assertEqual(doc.status, DocumentStatus.PENDING)
        self.assertEqual(doc.chunk_count, 0)

        updated = self.repo.update_ingest_result(
            doc.id,
            chunk_count=3,
            status=DocumentStatus.SUCCESS,
        )

        # 返回值：chunk_count + status 同时生效
        self.assertEqual(updated.chunk_count, 3)
        self.assertEqual(updated.status, DocumentStatus.SUCCESS)

        # 二次查询验证 DB 持久化（同一事务已 commit）
        refetched = self.repo.get_document(doc.id, plugin_id=PLUGIN_A)
        self.assertEqual(refetched.chunk_count, 3)
        self.assertEqual(refetched.status, DocumentStatus.SUCCESS)

    def test_update_ingest_result_chunk_count_keeps_old_value_on_invalid_status(
        self,
    ) -> None:
        """update_ingest_result：非法 status 抛异常，DB 中 chunk_count 保持原值。"""
        doc = self.repo.create_document(
            filename="ingest_invalid.pdf",
            file_path="/data/ingest_invalid.pdf",
            plugin_id=PLUGIN_A,
        )
        self.repo.update_status(doc.id, DocumentStatus.PROCESSING)

        with self.assertRaises(DocumentOperationError):
            self.repo.update_ingest_result(
                doc.id,
                chunk_count=5,
                status="INVALID_STATUS",
            )

        # 校验前置校验未发 SQL：status 与 chunk_count 均保持原值
        refetched = self.repo.get_document(doc.id, plugin_id=PLUGIN_A)
        self.assertEqual(refetched.status, DocumentStatus.PROCESSING)
        self.assertEqual(refetched.chunk_count, 0)

    def test_update_ingest_result_not_found(self) -> None:
        """update_ingest_result：不存在的主键抛 DocumentNotFoundError。"""
        with self.assertRaises(DocumentNotFoundError):
            self.repo.update_ingest_result(
                66666,
                chunk_count=1,
                status=DocumentStatus.SUCCESS,
            )

    # ---------------------------------------------------------- update_failure
    def test_update_failure_sets_failed_and_error_message(self) -> None:
        """update_failure：单事务设置 FAILED + error_message，不动 chunk_count。"""
        doc = self.repo.create_document(
            filename="fail.txt",
            file_path="/data/fail.txt",
            plugin_id=PLUGIN_A,
            file_size=5,
            mime_type="text/plain",
        )
        # 前置状态：PENDING / chunk_count=0 / error_message=None
        self.assertEqual(doc.status, DocumentStatus.PENDING)
        self.assertEqual(doc.chunk_count, 0)
        self.assertIsNone(doc.error_message)

        updated = self.repo.update_failure(
            doc.id,
            error_message="DocumentParserError: read failed",
        )

        # 返回值：FAILED + error_message 写入；chunk_count 不被修改
        self.assertEqual(updated.status, DocumentStatus.FAILED)
        self.assertEqual(updated.error_message, "DocumentParserError: read failed")
        self.assertEqual(updated.chunk_count, 0)

        # 二次查询验证 DB 持久化
        refetched = self.repo.get_document(doc.id, plugin_id=PLUGIN_A)
        self.assertEqual(refetched.status, DocumentStatus.FAILED)
        self.assertEqual(refetched.error_message, "DocumentParserError: read failed")
        self.assertEqual(refetched.chunk_count, 0)

    def test_update_failure_not_found(self) -> None:
        """update_failure：不存在的主键抛 DocumentNotFoundError。"""
        with self.assertRaises(DocumentNotFoundError):
            self.repo.update_failure(55555, error_message="boom")

    # ------------------------------------------------------------------ delete
    def test_delete_document_success(self) -> None:
        """delete_document：正常删除返回被删对象，get 抛 NotFound。"""
        doc = self.repo.create_document(
            filename="del.pdf",
            file_path="/data/del.pdf",
            plugin_id=PLUGIN_A,
        )

        deleted = self.repo.delete_document(doc.id, plugin_id=PLUGIN_A)

        # 返回被删除的 Document（detached，属性可读）
        self.assertEqual(deleted.id, doc.id)
        self.assertEqual(deleted.filename, "del.pdf")

        with self.assertRaises(DocumentNotFoundError):
            self.repo.get_document(doc.id, plugin_id=PLUGIN_A)

    def test_delete_document_not_found(self) -> None:
        """delete_document：不存在的主键抛 DocumentNotFoundError。"""
        with self.assertRaises(DocumentNotFoundError):
            self.repo.delete_document(77777, plugin_id=PLUGIN_A)

    # ----------------------------------------------- expire_on_commit / detached
    def test_detached_orm_attributes_accessible(self) -> None:
        """
        验证 expire_on_commit=False：create_document 返回的 detached ORM
        对象属性在 session close 后仍可读，无 DetachedInstanceError。
        """
        doc = self.repo.create_document(
            filename="detached.pdf",
            file_path="/data/detached.pdf",
            plugin_id=PLUGIN_A,
        )

        # 此时 session 已 close，doc 为 detached ORM；
        # expire_on_commit=False 保证以下属性均可读
        self.assertEqual(doc.filename, "detached.pdf")
        self.assertEqual(doc.status, DocumentStatus.PENDING)
        self.assertEqual(doc.chunk_count, 0)
        self.assertIsNotNone(doc.created_at)
        self.assertIsNotNone(doc.updated_at)

    # ----------------------------------------------------- protocol conformance
    def test_protocol_runtime_checkable(self) -> None:
        """验证 DocumentRepositoryImpl 实例 isinstance DocumentRepository。"""
        self.assertIsInstance(self.repo, DocumentRepository)

    # ================================================================
    # Phase 3.5 Step 2-B：plugin-aware 隔离测试
    # ================================================================
    def test_create_document_requires_plugin_id(self) -> None:
        """plugin-aware：create_document 必须显式带 plugin_id（不传 → TypeError）。"""
        with self.assertRaises(TypeError):
            self.repo.create_document(
                filename="no_plugin.txt",
                file_path="/data/no_plugin.txt",
            )

    def test_create_document_none_plugin_id_rejected(self) -> None:
        """plugin-aware：显式 plugin_id=None → NOT NULL 约束冲突（DocumentOperationError）。"""
        with self.assertRaises(DocumentOperationError):
            self.repo.create_document(
                filename="none_plugin.txt",
                file_path="/data/none_plugin.txt",
                plugin_id=None,
            )

    def test_get_document_plugin_isolation(self) -> None:
        """plugin-aware：A 只能读取自己的文档（WHERE id + plugin_id）。"""
        doc_a = self.repo.create_document(
            filename="a.txt", file_path="/data/a.txt", plugin_id=PLUGIN_A
        )
        doc_b = self.repo.create_document(
            filename="b.txt", file_path="/data/b.txt", plugin_id=PLUGIN_B
        )

        # A 读自己的 → 正常返回
        fetched = self.repo.get_document(doc_a.id, plugin_id=PLUGIN_A)
        self.assertEqual(fetched.id, doc_a.id)

        # B 用自己的 plugin_id 可读自己的 → 正常返回
        fetched_b = self.repo.get_document(doc_b.id, plugin_id=PLUGIN_B)
        self.assertEqual(fetched_b.id, doc_b.id)

    def test_get_document_cross_plugin_not_found(self) -> None:
        """plugin-aware：A get B 的 document_id → DocumentNotFoundError（不泄露归属）。"""
        doc_b = self.repo.create_document(
            filename="b.txt", file_path="/data/b.txt", plugin_id=PLUGIN_B
        )
        with self.assertRaises(DocumentNotFoundError):
            self.repo.get_document(doc_b.id, plugin_id=PLUGIN_A)

    def test_get_documents_by_ids_plugin_isolation(self) -> None:
        """plugin-aware：get_documents_by_ids 只返回 A 的文档（SQL 层过滤，不返回 B）。"""
        doc_a1 = self.repo.create_document(
            filename="a1.txt", file_path="/data/a1.txt", plugin_id=PLUGIN_A
        )
        doc_b = self.repo.create_document(
            filename="b.txt", file_path="/data/b.txt", plugin_id=PLUGIN_B
        )
        doc_a2 = self.repo.create_document(
            filename="a2.txt", file_path="/data/a2.txt", plugin_id=PLUGIN_A
        )

        results = self.repo.get_documents_by_ids(
            [doc_a1.id, doc_b.id, doc_a2.id], plugin_id=PLUGIN_A
        )
        ids = {d.id for d in results}
        self.assertEqual(ids, {doc_a1.id, doc_a2.id})
        self.assertNotIn(doc_b.id, ids)

    def test_get_documents_by_ids_empty_ids(self) -> None:
        """plugin-aware：空 ids 列表 → []（不发 SQL）。"""
        self.assertEqual(self.repo.get_documents_by_ids([], plugin_id=PLUGIN_A), [])

    def test_get_success_document_ids_plugin_isolation(self) -> None:
        """plugin-aware：只返回 A 的 SUCCESS id；A 的非 SUCCESS 与 B 的 SUCCESS 均排除。"""
        doc_a_s1 = self.repo.create_document(
            filename="s1.txt", file_path="/data/s1.txt", plugin_id=PLUGIN_A
        )
        doc_a_p = self.repo.create_document(
            filename="p.txt", file_path="/data/p.txt", plugin_id=PLUGIN_A
        )
        doc_b_s = self.repo.create_document(
            filename="bs.txt", file_path="/data/bs.txt", plugin_id=PLUGIN_B
        )
        doc_a_s2 = self.repo.create_document(
            filename="s2.txt", file_path="/data/s2.txt", plugin_id=PLUGIN_A
        )

        self.repo.update_status(doc_a_s1.id, DocumentStatus.SUCCESS)
        self.repo.update_status(doc_b_s.id, DocumentStatus.SUCCESS)
        self.repo.update_status(doc_a_s2.id, DocumentStatus.SUCCESS)
        # doc_a_p 保持 PENDING

        ids = self.repo.get_success_document_ids(plugin_id=PLUGIN_A)
        self.assertEqual(sorted(ids), sorted([doc_a_s1.id, doc_a_s2.id]))
        self.assertNotIn(doc_b_s.id, ids)
        self.assertNotIn(doc_a_p.id, ids)

    def test_get_success_document_ids_no_results(self) -> None:
        """plugin-aware：无 SUCCESS → 返回 []（不抛异常）。"""
        doc = self.repo.create_document(
            filename="p.txt", file_path="/data/p.txt", plugin_id=PLUGIN_A
        )
        self.repo.update_status(doc.id, DocumentStatus.FAILED)
        self.assertEqual(self.repo.get_success_document_ids(plugin_id=PLUGIN_A), [])

    def test_delete_document_cross_plugin_not_found(self) -> None:
        """plugin-aware：A 删除 B 的文档 → DocumentNotFoundError，B 的文档不被删除。"""
        doc_b = self.repo.create_document(
            filename="b.txt", file_path="/data/b.txt", plugin_id=PLUGIN_B
        )

        with self.assertRaises(DocumentNotFoundError):
            self.repo.delete_document(doc_b.id, plugin_id=PLUGIN_A)

        # B 的文档仍然存在
        fetched = self.repo.get_document(doc_b.id, plugin_id=PLUGIN_B)
        self.assertEqual(fetched.id, doc_b.id)

    def test_update_helpers_unchanged_by_plugin_aware(self) -> None:
        """plugin-aware：update_status / update_ingest_result / update_failure 不受影响。"""
        doc = self.repo.create_document(
            filename="life.txt", file_path="/data/life.txt", plugin_id=PLUGIN_A
        )

        self.repo.update_status(doc.id, DocumentStatus.PROCESSING)
        self.repo.update_ingest_result(
            doc.id, chunk_count=2, status=DocumentStatus.SUCCESS
        )
        updated = self.repo.get_document(doc.id, plugin_id=PLUGIN_A)
        self.assertEqual(updated.status, DocumentStatus.SUCCESS)
        self.assertEqual(updated.chunk_count, 2)

        self.repo.update_failure(doc.id, error_message="boom")
        failed = self.repo.get_document(doc.id, plugin_id=PLUGIN_A)
        self.assertEqual(failed.status, DocumentStatus.FAILED)
        self.assertEqual(failed.error_message, "boom")

    # ================================================================
    # Phase 3.6 Step 2-D：list_documents / count_documents（我的知识库）
    # ================================================================
    def _create_webpage_doc(self, plugin_id, title, url):
        """构造网页剪藏文档（webpage + title + url）。"""
        return self.repo.create_document(
            filename="webclip.txt",
            file_path="",
            plugin_id=plugin_id,
            title=title,
            url=url,
            source_type="webpage",
        )

    def test_list_documents_plugin_a_isolation(self) -> None:
        """list_documents：A 列表只看到 A（SQL 层 plugin_id 过滤，不含 B）。"""
        self._create_webpage_doc(PLUGIN_A, "A1", "https://a/1")
        self._create_webpage_doc(PLUGIN_A, "A2", "https://a/2")
        self._create_webpage_doc(PLUGIN_B, "B1", "https://b/1")

        docs = self.repo.list_documents(PLUGIN_A)
        titles = {d.title for d in docs}
        self.assertEqual(titles, {"A1", "A2"})
        self.assertNotIn("B1", titles)

    def test_list_documents_plugin_b_isolation(self) -> None:
        """list_documents：B 列表只看到 B（反向隔离验证）。"""
        self._create_webpage_doc(PLUGIN_A, "A1", "https://a/1")
        self._create_webpage_doc(PLUGIN_B, "B1", "https://b/1")
        self._create_webpage_doc(PLUGIN_B, "B2", "https://b/2")

        docs = self.repo.list_documents(PLUGIN_B)
        titles = {d.title for d in docs}
        self.assertEqual(titles, {"B1", "B2"})
        self.assertNotIn("A1", titles)

    def test_list_documents_keyword_own_plugin_only(self) -> None:
        """list_documents：keyword 只能搜到当前 Plugin 的文档（A 搜不到 B 同名）。"""
        self._create_webpage_doc(PLUGIN_A, "共享标题", "https://a/1")
        self._create_webpage_doc(PLUGIN_B, "共享标题", "https://b/1")

        docs = self.repo.list_documents(PLUGIN_A, keyword="共享标题")
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].url, "https://a/1")

    def test_list_documents_keyword_matches_title_filename_url(self) -> None:
        """list_documents：keyword 匹配 title / filename / url 三个字段。"""
        self._create_webpage_doc(PLUGIN_A, "标题命中", "https://a/1")
        self.repo.create_document(
            filename="命中文件名.txt", file_path="/data", plugin_id=PLUGIN_A
        )
        self._create_webpage_doc(PLUGIN_A, "其它", "https://a/hit-here")

        by_title = self.repo.list_documents(PLUGIN_A, keyword="标题命中")
        self.assertEqual(len(by_title), 1)
        by_filename = self.repo.list_documents(PLUGIN_A, keyword="文件名")
        self.assertEqual(len(by_filename), 1)
        by_url = self.repo.list_documents(PLUGIN_A, keyword="hit-here")
        self.assertEqual(len(by_url), 1)

    def test_list_documents_blank_keyword_ignored(self) -> None:
        """list_documents：纯空白 keyword 视为未提供，不增加 LIKE 条件。"""
        self._create_webpage_doc(PLUGIN_A, "alpha", "https://a/1")
        docs = self.repo.list_documents(PLUGIN_A, keyword="   ")
        self.assertEqual(len(docs), 1)
        self.assertEqual(self.repo.count_documents(PLUGIN_A, keyword="   "), 1)

    def test_list_documents_keyword_percent_escaped(self) -> None:
        """list_documents：keyword 含 % 转义为字面匹配，不扩大匹配范围。"""
        self._create_webpage_doc(PLUGIN_A, "进度 100%", "https://a/1")
        self._create_webpage_doc(PLUGIN_A, "普通文档", "https://a/2")

        docs = self.repo.list_documents(PLUGIN_A, keyword="%")
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].title, "进度 100%")

    def test_list_documents_keyword_underscore_escaped(self) -> None:
        """list_documents：keyword 含 _ 转义为字面匹配，不把 _ 当任意单字符。"""
        self._create_webpage_doc(PLUGIN_A, "a_b.txt", "https://a/1")
        self._create_webpage_doc(PLUGIN_A, "axb.txt", "https://a/2")

        docs = self.repo.list_documents(PLUGIN_A, keyword="a_b")
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].title, "a_b.txt")

    def test_list_documents_keyword_backslash_escaped(self) -> None:
        """list_documents：keyword 含 \\ 转义为字面匹配反斜杠。"""
        self._create_webpage_doc(PLUGIN_A, "c\\d.txt", "https://a/1")
        self._create_webpage_doc(PLUGIN_A, "cd.txt", "https://a/2")

        docs = self.repo.list_documents(PLUGIN_A, keyword="c\\d")
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].title, "c\\d.txt")

    def test_list_documents_status_filter(self) -> None:
        """list_documents：status 精确筛选且跨 Plugin 隔离。"""
        d1 = self._create_webpage_doc(PLUGIN_A, "s1", "https://a/1")
        d2 = self._create_webpage_doc(PLUGIN_A, "s2", "https://a/2")
        self._create_webpage_doc(PLUGIN_B, "s3", "https://b/1")
        self.repo.update_status(d1.id, DocumentStatus.SUCCESS)
        self.repo.update_status(d2.id, DocumentStatus.FAILED)

        docs = self.repo.list_documents(PLUGIN_A, status="SUCCESS")
        self.assertEqual([d.id for d in docs], [d1.id])
        self.assertEqual(self.repo.count_documents(PLUGIN_A, status="SUCCESS"), 1)
        self.assertEqual(self.repo.count_documents(PLUGIN_A, status="FAILED"), 1)

    def test_list_documents_source_type_filter(self) -> None:
        """list_documents：source_type 精确筛选（webpage/upload）且跨 Plugin 隔离。"""
        self._create_webpage_doc(PLUGIN_A, "web", "https://a/1")
        self.repo.create_document(
            filename="up.txt", file_path="/data/up.txt", plugin_id=PLUGIN_A
        )
        self._create_webpage_doc(PLUGIN_B, "web-b", "https://b/1")

        webpage_docs = self.repo.list_documents(PLUGIN_A, source_type="webpage")
        self.assertEqual(len(webpage_docs), 1)
        self.assertEqual(webpage_docs[0].title, "web")
        self.assertEqual(self.repo.count_documents(PLUGIN_A, source_type="upload"), 1)

    def test_count_documents_plugin_isolation(self) -> None:
        """count_documents：只统计当前 Plugin，不统计其它 Plugin。"""
        self._create_webpage_doc(PLUGIN_A, "A1", "https://a/1")
        self._create_webpage_doc(PLUGIN_A, "A2", "https://a/2")
        self._create_webpage_doc(PLUGIN_B, "B1", "https://b/1")

        self.assertEqual(self.repo.count_documents(PLUGIN_A), 2)
        self.assertEqual(self.repo.count_documents(PLUGIN_B), 1)

    def test_count_documents_no_results(self) -> None:
        """count_documents：无匹配返回 0（不抛异常）。"""
        self.assertEqual(self.repo.count_documents(PLUGIN_A), 0)

    def test_list_documents_empty_result(self) -> None:
        """list_documents：无匹配返回 []（不抛异常）。"""
        self._create_webpage_doc(PLUGIN_B, "B1", "https://b/1")
        self.assertEqual(self.repo.list_documents(PLUGIN_A), [])

    def test_list_documents_pagination_ordering(self) -> None:
        """list_documents：分页正确（LIMIT/OFFSET），排序 created_at DESC, id DESC。"""
        created = []
        for i in range(5):
            created.append(
                self.repo.create_document(
                    filename=f"page_{i}.txt", file_path="/data", plugin_id=PLUGIN_A
                )
            )

        # SQLite created_at 秒级精度：同秒插入时以 id DESC 次级排序保证顺序稳定
        page1 = self.repo.list_documents(PLUGIN_A, page=1, page_size=2)
        self.assertEqual([d.id for d in page1], [created[4].id, created[3].id])
        page2 = self.repo.list_documents(PLUGIN_A, page=2, page_size=2)
        self.assertEqual([d.id for d in page2], [created[2].id, created[1].id])
        page3 = self.repo.list_documents(PLUGIN_A, page=3, page_size=2)
        self.assertEqual([d.id for d in page3], [created[0].id])

    def test_list_documents_pagination_does_not_break_plugin_isolation(self) -> None:
        """list_documents：分页 + plugin_id 过滤组合下仍只返回 A。"""
        for i in range(3):
            self._create_webpage_doc(PLUGIN_A, f"A{i}", f"https://a/{i}")
        self._create_webpage_doc(PLUGIN_B, "B1", "https://b/1")

        docs = self.repo.list_documents(PLUGIN_A, page=1, page_size=2)
        self.assertEqual(len(docs), 2)
        titles = {d.title for d in docs}
        self.assertNotIn("B1", titles)

    def test_list_count_conditions_consistent(self) -> None:
        """count 与 list 使用完全一致的过滤条件（keyword/status/source_type 组合）。"""
        for i in range(5):
            self._create_webpage_doc(PLUGIN_A, f"alpha-{i}", f"https://a/{i}")
        self._create_webpage_doc(PLUGIN_B, "alpha-x", "https://b/x")

        self.assertEqual(self.repo.count_documents(PLUGIN_A, keyword="alpha"), 5)
        self.assertEqual(
            len(
                self.repo.list_documents(
                    PLUGIN_A, keyword="alpha", page=1, page_size=100
                )
            ),
            5,
        )

        # 组合 status 后条件仍一致
        first = self.repo.list_documents(PLUGIN_A, keyword="alpha", page=1, page_size=1)[0]
        self.repo.update_status(first.id, DocumentStatus.SUCCESS)
        self.assertEqual(
            self.repo.count_documents(PLUGIN_A, keyword="alpha", status="SUCCESS"), 1
        )
        self.assertEqual(
            len(
                self.repo.list_documents(
                    PLUGIN_A, keyword="alpha", status="SUCCESS", page=1, page_size=100
                )
            ),
            1,
        )

    def test_list_documents_protocol_conformance(self) -> None:
        """Protocol runtime_checkable：Impl 实例提供 list/count 契约。"""
        self.assertIsInstance(self.repo, DocumentRepository)

    def test_get_webpage_by_url_is_exact_and_plugin_scoped(self) -> None:
        own = self._create_webpage_doc(PLUGIN_A, "own", "https://same/page")
        self._create_webpage_doc(PLUGIN_B, "other", "https://same/page")
        self._create_webpage_doc(PLUGIN_A, "similar", "https://same/page?x=1")

        result = self.repo.get_webpage_by_url(PLUGIN_A, "https://same/page")

        self.assertIsNotNone(result)
        self.assertEqual(result.id, own.id)
        self.assertEqual(result.plugin_id, PLUGIN_A)

    def test_update_webpage_metadata_keeps_identity(self) -> None:
        document = self._create_webpage_doc(PLUGIN_A, "old", "https://old")
        updated = self.repo.update_webpage_metadata(
            document.id, title="new", url="https://new"
        )

        self.assertEqual(updated.id, document.id)
        self.assertEqual(updated.plugin_id, PLUGIN_A)
        self.assertEqual(updated.title, "new")
        self.assertEqual(updated.url, "https://new")


if __name__ == "__main__":
    unittest.main()
