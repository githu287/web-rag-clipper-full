"""
DocumentRepositoryImpl 单元测试（Phase 2.9 Step 1；Phase 3.1 Step 3 扩展
title / url / source_type 来源元数据保存；Phase 3.4 Step C user-aware 升级）。

测试策略：
1) 用 SQLite in-memory engine + StaticPool 替换真实 MySQL，避免外部依赖；
2) 每个测试方法 setUp 重建 engine + create_all，保证方法间数据隔离；
3) 覆盖 4 个 CRUD 方法的正常路径 + 异常路径 + 边界条件；
4) 验证 expire_on_commit=False：commit 后 detached ORM 属性可读；
5) 验证 Protocol runtime_checkable：Impl 实例 isinstance DocumentRepository。

Phase 3.1 Step 3 新增覆盖：
- create_document 显式传 title/url/source_type 正确保存；
- 旧调用（不传 3 个新参数）保持兼容：title/url=NULL、source_type 默认 upload；
- title=None 显式传与缺省等价，url 独立落库。

Phase 3.4 Step C 新增覆盖（user-aware）：
- create_document：user_id 必填（不传 → TypeError；None → NOT NULL 冲突）；
- get_document(document_id, user_id)：A 只能读 A；A 读 B → DocumentNotFoundError；
- get_documents_by_ids(document_ids, user_id)：SQL 层过滤只返回 A；空 ids → []；
- get_success_document_ids(user_id)：只返回 A 的 SUCCESS id；
- delete_document(document_id, user_id)：A 删 B → DocumentNotFoundError，B 的文档仍在；
- update_status / update_ingest_result / update_failure 不受 user-aware 影响。

不依赖：
- 真实 MySQL（SQLite in-memory 替代）
- 真实 .env（无 Settings 依赖，直接构造 engine）
- FastAPI / Service / API
"""

from __future__ import annotations

import unittest
from datetime import datetime

from sqlalchemy import Engine, create_engine
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


class DocumentRepositoryImplTest(unittest.TestCase):
    """DocumentRepositoryImpl CRUD 单元测试。"""

    def setUp(self) -> None:
        """每个测试方法重建 engine + create_all，保证数据隔离。"""
        self.engine: Engine = _make_test_engine()
        Base.metadata.create_all(self.engine)
        self.repo: DocumentRepositoryImpl = DocumentRepositoryImpl(self.engine)

    def tearDown(self) -> None:
        """测试结束销毁表，释放 in-memory DB。"""
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    # ------------------------------------------------------------------ create
    def test_create_document_defaults(self) -> None:
        """create_document：默认 status=PENDING、chunk_count=0、user_id 必填落库。"""
        doc = self.repo.create_document(
            filename="test.pdf",
            file_path="/data/test.pdf",
            user_id=1,
        )

        self.assertIsNotNone(doc.id)
        self.assertGreater(doc.id, 0)
        self.assertEqual(doc.filename, "test.pdf")
        self.assertEqual(doc.file_path, "/data/test.pdf")
        self.assertEqual(doc.user_id, 1)
        self.assertEqual(doc.status, DocumentStatus.PENDING)
        self.assertEqual(doc.chunk_count, 0)
        self.assertIsInstance(doc.created_at, datetime)
        self.assertIsInstance(doc.updated_at, datetime)
        # 不严格断言 updated_at > created_at（SQLite 时间精度可能相同）
        self.assertGreaterEqual(doc.updated_at, doc.created_at)

    def test_create_document_with_user_id(self) -> None:
        """create_document：显式传入 user_id。"""
        doc = self.repo.create_document(
            filename="user_doc.pdf",
            file_path="/data/user_doc.pdf",
            user_id=42,
        )

        self.assertEqual(doc.user_id, 42)
        self.assertEqual(doc.filename, "user_doc.pdf")

    def test_create_document_with_file_metadata(self) -> None:
        """create_document：显式传入 file_size / mime_type（Phase 2.10 Step 3）。"""
        doc = self.repo.create_document(
            filename="meta.txt",
            file_path="/data/meta.txt",
            user_id=3,
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
            user_id=3,
        )

        self.assertEqual(doc.file_size, 0)
        self.assertEqual(doc.mime_type, "")

    # ------------------------------------------- Phase 3.1 Step 3：来源元数据
    def test_create_document_with_source_metadata(self) -> None:
        """create_document：显式传入 title / url / source_type（网页剪藏）。"""
        doc = self.repo.create_document(
            filename="webclip.txt",
            file_path="",
            user_id=2,
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
        refetched = self.repo.get_document(doc.id, user_id=2)
        self.assertEqual(refetched.title, "示例文章")
        self.assertEqual(refetched.url, "https://example.com/article/1")
        self.assertEqual(refetched.source_type, "webpage")

    def test_create_document_default_source_metadata(self) -> None:
        """create_document：不传 title/url/source_type → NULL / 默认 upload（旧调用兼容）。"""
        doc = self.repo.create_document(
            filename="legacy.txt",
            file_path="/data/legacy.txt",
            user_id=2,
        )

        self.assertIsNone(doc.title)
        self.assertIsNone(doc.url)
        self.assertEqual(doc.source_type, "upload")

        refetched = self.repo.get_document(doc.id, user_id=2)
        self.assertIsNone(refetched.title)
        self.assertIsNone(refetched.url)
        self.assertEqual(refetched.source_type, "upload")

    def test_create_document_title_none_url_kept(self) -> None:
        """create_document：title=None 显式传与缺省等价；url 独立落库。"""
        doc = self.repo.create_document(
            filename="webclip2.txt",
            file_path="",
            user_id=2,
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
            user_id=7,
        )

        fetched = self.repo.get_document(created.id, user_id=7)

        self.assertEqual(fetched.id, created.id)
        self.assertEqual(fetched.filename, "get.pdf")
        self.assertEqual(fetched.file_path, "/data/get.pdf")
        self.assertEqual(fetched.user_id, 7)
        self.assertEqual(fetched.status, DocumentStatus.PENDING)
        self.assertEqual(fetched.chunk_count, 0)

    def test_get_document_not_found(self) -> None:
        """get_document：不存在的主键抛 DocumentNotFoundError。"""
        with self.assertRaises(DocumentNotFoundError):
            self.repo.get_document(99999, user_id=1)

    # ----------------------------------------------------------- update_status
    def test_update_status_all_states(self) -> None:
        """update_status：遍历 DocumentStatus.ALL 所有合法状态。"""
        doc = self.repo.create_document(
            filename="status.pdf",
            file_path="/data/status.pdf",
            user_id=1,
        )

        for status in DocumentStatus.ALL:
            updated = self.repo.update_status(doc.id, status)
            self.assertEqual(updated.status, status)

            # 二次查询验证 DB 持久化
            refetched = self.repo.get_document(doc.id, user_id=1)
            self.assertEqual(refetched.status, status)

    def test_update_status_invalid(self) -> None:
        """update_status：非法 status 抛 DocumentOperationError，DB 不变。"""
        doc = self.repo.create_document(
            filename="invalid.pdf",
            file_path="/data/invalid.pdf",
            user_id=1,
        )

        with self.assertRaises(DocumentOperationError):
            self.repo.update_status(doc.id, "INVALID_STATUS")

        # 验证 DB 仍为默认 PENDING（未发 SQL）
        refetched = self.repo.get_document(doc.id, user_id=1)
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
            user_id=1,
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
        refetched = self.repo.get_document(doc.id, user_id=1)
        self.assertEqual(refetched.chunk_count, 3)
        self.assertEqual(refetched.status, DocumentStatus.SUCCESS)

    def test_update_ingest_result_chunk_count_keeps_old_value_on_invalid_status(
        self,
    ) -> None:
        """update_ingest_result：非法 status 抛异常，DB 中 chunk_count 保持原值。"""
        doc = self.repo.create_document(
            filename="ingest_invalid.pdf",
            file_path="/data/ingest_invalid.pdf",
            user_id=1,
        )
        self.repo.update_status(doc.id, DocumentStatus.PROCESSING)

        with self.assertRaises(DocumentOperationError):
            self.repo.update_ingest_result(
                doc.id,
                chunk_count=5,
                status="INVALID_STATUS",
            )

        # 校验前置校验未发 SQL：status 与 chunk_count 均保持原值
        refetched = self.repo.get_document(doc.id, user_id=1)
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
            user_id=1,
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
        refetched = self.repo.get_document(doc.id, user_id=1)
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
            user_id=1,
        )

        deleted = self.repo.delete_document(doc.id, user_id=1)

        # 返回被删除的 Document（detached，属性可读）
        self.assertEqual(deleted.id, doc.id)
        self.assertEqual(deleted.filename, "del.pdf")

        with self.assertRaises(DocumentNotFoundError):
            self.repo.get_document(doc.id, user_id=1)

    def test_delete_document_not_found(self) -> None:
        """delete_document：不存在的主键抛 DocumentNotFoundError。"""
        with self.assertRaises(DocumentNotFoundError):
            self.repo.delete_document(77777, user_id=1)

    # ----------------------------------------------- expire_on_commit / detached
    def test_detached_orm_attributes_accessible(self) -> None:
        """
        验证 expire_on_commit=False：create_document 返回的 detached ORM
        对象属性在 session close 后仍可读，无 DetachedInstanceError。
        """
        doc = self.repo.create_document(
            filename="detached.pdf",
            file_path="/data/detached.pdf",
            user_id=1,
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
    # Phase 3.4 Step C：user-aware 隔离测试
    # ================================================================
    def test_create_document_requires_user_id(self) -> None:
        """Step C：create_document 必须显式带 user_id（不传 → TypeError）。"""
        with self.assertRaises(TypeError):
            self.repo.create_document(
                filename="no_user.txt",
                file_path="/data/no_user.txt",
            )

    def test_create_document_none_user_id_rejected(self) -> None:
        """Step C：显式 user_id=None → NOT NULL 约束冲突（DocumentOperationError）。"""
        with self.assertRaises(DocumentOperationError):
            self.repo.create_document(
                filename="none_user.txt",
                file_path="/data/none_user.txt",
                user_id=None,
            )

    def test_get_document_user_isolation(self) -> None:
        """Step C：A(1) 只能读取自己的文档（WHERE id + user_id）。"""
        doc_a = self.repo.create_document(
            filename="a.txt", file_path="/data/a.txt", user_id=1
        )
        doc_b = self.repo.create_document(
            filename="b.txt", file_path="/data/b.txt", user_id=2
        )

        # A 读自己的 → 正常返回
        fetched = self.repo.get_document(doc_a.id, user_id=1)
        self.assertEqual(fetched.id, doc_a.id)

        # A 读 B 的 → 正常返回（B 用自己的 user_id 可读）
        fetched_b = self.repo.get_document(doc_b.id, user_id=2)
        self.assertEqual(fetched_b.id, doc_b.id)

    def test_get_document_cross_user_not_found(self) -> None:
        """Step C：A(1) get B(2) 的 document_id → DocumentNotFoundError（不泄露归属）。"""
        doc_b = self.repo.create_document(
            filename="b.txt", file_path="/data/b.txt", user_id=2
        )
        with self.assertRaises(DocumentNotFoundError):
            self.repo.get_document(doc_b.id, user_id=1)

    def test_get_documents_by_ids_user_isolation(self) -> None:
        """Step C：get_documents_by_ids 只返回当前用户（A）的文档（SQL 层过滤）。"""
        doc_a1 = self.repo.create_document(
            filename="a1.txt", file_path="/data/a1.txt", user_id=1
        )
        doc_b = self.repo.create_document(
            filename="b.txt", file_path="/data/b.txt", user_id=2
        )
        doc_a2 = self.repo.create_document(
            filename="a2.txt", file_path="/data/a2.txt", user_id=1
        )

        results = self.repo.get_documents_by_ids(
            [doc_a1.id, doc_b.id, doc_a2.id], user_id=1
        )
        ids = {d.id for d in results}
        self.assertEqual(ids, {doc_a1.id, doc_a2.id})
        self.assertNotIn(doc_b.id, ids)

    def test_get_documents_by_ids_empty_ids(self) -> None:
        """Step C：空 ids 列表 → []（不发 SQL）。"""
        self.assertEqual(self.repo.get_documents_by_ids([], user_id=1), [])

    def test_get_success_document_ids_user_isolation(self) -> None:
        """Step C：只返回 A 的 SUCCESS id；B 的 SUCCESS 与 A 的非 SUCCESS 均排除。"""
        doc_a_s1 = self.repo.create_document(
            filename="s1.txt", file_path="/data/s1.txt", user_id=1
        )
        doc_a_p = self.repo.create_document(
            filename="p.txt", file_path="/data/p.txt", user_id=1
        )
        doc_b_s = self.repo.create_document(
            filename="bs.txt", file_path="/data/bs.txt", user_id=2
        )
        doc_a_s2 = self.repo.create_document(
            filename="s2.txt", file_path="/data/s2.txt", user_id=1
        )

        self.repo.update_status(doc_a_s1.id, DocumentStatus.SUCCESS)
        self.repo.update_status(doc_b_s.id, DocumentStatus.SUCCESS)
        self.repo.update_status(doc_a_s2.id, DocumentStatus.SUCCESS)
        # doc_a_p 保持 PENDING

        ids = self.repo.get_success_document_ids(user_id=1)
        self.assertEqual(sorted(ids), sorted([doc_a_s1.id, doc_a_s2.id]))
        self.assertNotIn(doc_b_s.id, ids)
        self.assertNotIn(doc_a_p.id, ids)

    def test_get_success_document_ids_no_results(self) -> None:
        """Step C：无 SUCCESS → 返回 []（不抛异常）。"""
        doc = self.repo.create_document(
            filename="p.txt", file_path="/data/p.txt", user_id=1
        )
        self.repo.update_status(doc.id, DocumentStatus.FAILED)
        self.assertEqual(self.repo.get_success_document_ids(user_id=1), [])

    def test_delete_document_cross_user_not_found(self) -> None:
        """Step C：A(1) 删除 B(2) 的文档 → DocumentNotFoundError，B 的文档不被删除。"""
        doc_b = self.repo.create_document(
            filename="b.txt", file_path="/data/b.txt", user_id=2
        )

        with self.assertRaises(DocumentNotFoundError):
            self.repo.delete_document(doc_b.id, user_id=1)

        # B 的文档仍然存在
        fetched = self.repo.get_document(doc_b.id, user_id=2)
        self.assertEqual(fetched.id, doc_b.id)

    def test_update_helpers_unchanged_by_user_aware(self) -> None:
        """Step C：update_status / update_ingest_result / update_failure 不受影响。"""
        doc = self.repo.create_document(
            filename="life.txt", file_path="/data/life.txt", user_id=1
        )

        self.repo.update_status(doc.id, DocumentStatus.PROCESSING)
        self.repo.update_ingest_result(
            doc.id, chunk_count=2, status=DocumentStatus.SUCCESS
        )
        updated = self.repo.get_document(doc.id, user_id=1)
        self.assertEqual(updated.status, DocumentStatus.SUCCESS)
        self.assertEqual(updated.chunk_count, 2)

        self.repo.update_failure(doc.id, error_message="boom")
        failed = self.repo.get_document(doc.id, user_id=1)
        self.assertEqual(failed.status, DocumentStatus.FAILED)
        self.assertEqual(failed.error_message, "boom")


if __name__ == "__main__":
    unittest.main()
