"""
DocumentDeleteService 单元测试（Phase 2.11 Step 2；Phase 3.5 Step 2-E workspace-aware）。

技术栈：unittest + unittest.mock（不引入 pytest）。
注入方式：Mock(spec=DocumentRepository) + Mock(spec=MilvusRepository) +
Mock(spec=FileStorage)，符合 Protocol 注入约定。

Phase 3.5 Step 2-E 变更（workspace-aware，user_id → plugin_id）：
  - delete_document(document_id, plugin_id)：Step 1 get_document(document_id,
    plugin_id) ownership check；Step 6 delete_document(document_id, plugin_id)；
  - A delete A：完整删除流程，get_document/delete_document 均带 plugin_id；
  - A delete B（跨 Workspace）：get_document 抛 DocumentNotFoundError → 幂等成功
    （按「不存在」处理，不泄露归属），后续步骤（Milvus/FileStorage/MySQL）
    均不执行；
  - Milvus → FileStorage → MySQL 删除顺序不变。

覆盖用例：
  A. 删除顺序：get_document → update_status(DELETING) → query_page_chunks →
     delete_chunks → FileStorage.delete → delete_document（MySQL 最终提交点）。
  B. webpage 来源：不调用 FileStorage.delete。
  C. upload 来源：调用 FileStorage.delete，且以实际物理路径删除。
  D. webpage + FileStorage 抛错：不触发 FileStorage.delete（不误删网页文档）。
  E. MySQL delete_document 抛 DocumentNotFoundError（并发删除）→ 重试收敛成功。
  F. get_document NotFound → 幂等成功（不写 DELETING、不触碰 Milvus / 文件系统）。
  G. Protocol 注入。
  H. 回归：update_status 状态机约束不受 workspace-aware 影响（SQLite）。
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import Mock, call

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from backend.core.exceptions import DocumentNotFoundError
from backend.models.base import Base
from backend.models.document import DocumentStatus
from backend.repositories.mysql import DocumentRepository
from backend.repositories.milvus import MilvusRepository
from backend.services.document_delete import DocumentDeleteService
from backend.storage.protocol import FileStorage
from backend.repositories.mysql import DocumentRepositoryImpl

# Phase 3.5 Step 2-B：回归测试预置的 Plugin Workspace 标识
_PLUGIN_ID = "plugin-r-4f0d1c2b3a99887766554433221100ffeeddccbbaa"


class DocumentDeleteServiceTest(unittest.TestCase):
    """DocumentDeleteService 单元测试（mock 注入）。"""

    def setUp(self) -> None:
        """构造注入依赖（Mock(spec=...)）与一个可复用的假 Document。"""
        self.document_repo = Mock(spec=DocumentRepository)
        self.milvus_repo = Mock(spec=MilvusRepository)
        self.file_storage = Mock(spec=FileStorage)

        # Milvus chunk 查询返回具体列表（delete service 内部对结果做 len()）
        self.milvus_repo.query_page_chunks.return_value = [
            {"id": "chunk-1"},
            {"id": "chunk-2"},
        ]

        self.fake_document = Mock()
        self.fake_document.id = 7
        self.fake_document.status = DocumentStatus.PENDING
        self.fake_document.source_type = "upload"
        self.fake_document.file_path = "/data/upload.pdf"

        self.service = DocumentDeleteService(
            document_repository=self.document_repo,
            milvus_repository=self.milvus_repo,
            file_storage=self.file_storage,
        )

    def run_async(self, coro) -> object:
        """同步运行 async 测试体。"""
        return asyncio.run(coro)

    # ------------------------------------------------------------------ A. 删除顺序
    def test_delete_success_order(self) -> None:
        """A：Milvus → FileStorage → MySQL 严格顺序（Step 2-E：均带 plugin_id）。"""
        self.document_repo.get_document.return_value = self.fake_document

        async def scenario() -> None:
            await self.service.delete_document(7, plugin_id=_PLUGIN_ID)

        self.run_async(scenario())

        # 严格调用顺序（update_status(DELETING) 在 Milvus 查询之前）
        self.assertEqual(
            self.document_repo.mock_calls,
            [
                call.get_document(7, _PLUGIN_ID),
                call.update_status(7, DocumentStatus.DELETING),
                call.delete_document(7, _PLUGIN_ID),
            ],
        )
        # Milvus：query_page_chunks(7) → delete_chunks(chunk_ids)
        self.milvus_repo.query_page_chunks.assert_called_once_with(7)
        self.milvus_repo.delete_chunks.assert_called_once_with(
            [{"id": "chunk-1"}, {"id": "chunk-2"}]
        )
        # FileStorage：以实际物理路径删除
        self.file_storage.delete.assert_called_once_with("/data/upload.pdf")

    # ------------------------------------------------------ B. webpage 不删文件
    def test_delete_webpage_skips_file_storage(self) -> None:
        """B：webpage 来源（file_path 为空）不调用 FileStorage.delete。"""
        self.document_repo.get_document.return_value = self.fake_document
        self.fake_document.source_type = "webpage"
        self.fake_document.file_path = ""

        async def scenario() -> None:
            await self.service.delete_document(7, plugin_id=_PLUGIN_ID)

        self.run_async(scenario())

        # FileStorage 完全不被触碰
        self.file_storage.delete.assert_not_called()
        # MySQL 最终提交点仍执行（带 plugin_id）
        self.document_repo.delete_document.assert_called_once_with(7, _PLUGIN_ID)

    # ------------------------------------------------------ C. upload 删物理文件
    def test_delete_upload_still_deletes_file(self) -> None:
        """C：upload 来源 → FileStorage.delete 以实际物理路径调用（plugin_id 传参不变）。"""
        self.document_repo.get_document.return_value = self.fake_document
        self.fake_document.source_type = "upload"
        self.fake_document.file_path = "/data/upload.pdf"

        async def scenario() -> None:
            await self.service.delete_document(7, plugin_id=_PLUGIN_ID)

        self.run_async(scenario())

        # 物理文件删除逻辑不变
        self.file_storage.delete.assert_called_once_with("/data/upload.pdf")

    # ------------------------------------------------- D. webpage + FileStorage 抛错
    def test_delete_webpage_file_failure_not_triggered(self) -> None:
        """D：webpage 即使 FileStorage 会抛错也不触发（source_type 判断优先）。"""
        self.document_repo.get_document.return_value = self.fake_document
        self.fake_document.source_type = "webpage"
        self.fake_document.file_path = ""
        # 即使 delete 被调用也会抛错——但本场景不应触发
        self.file_storage.delete.side_effect = RuntimeError("should not be called")

        async def scenario() -> None:
            await self.service.delete_document(7, plugin_id=_PLUGIN_ID)

        self.run_async(scenario())

        # 不触发 FileStorage.delete（没有误删网页文档）
        self.file_storage.delete.assert_not_called()
        # MySQL 最终提交点执行
        self.document_repo.delete_document.assert_called_once_with(7, _PLUGIN_ID)

    # ------------------------------------------------- E. MySQL 删除失败重试收敛
    def test_delete_mysql_failure_retry_converges(self) -> None:
        """E：delete_document 抛 NotFound（并发已删）→ 重试收敛成功（幂等）。"""
        self.document_repo.get_document.return_value = self.fake_document
        # 第一次 delete_document 抛 NotFound，第二次成功
        self.document_repo.delete_document.side_effect = [
            DocumentNotFoundError("document not found: id=7"),
            None,
        ]

        async def scenario() -> None:
            await self.service.delete_document(7, plugin_id=_PLUGIN_ID)
            # 第二次调用（幂等重试）成功
            await self.service.delete_document(7, plugin_id=_PLUGIN_ID)

        self.run_async(scenario())

        # delete_document 被调用两次，均带 plugin_id
        self.assertEqual(
            self.document_repo.delete_document.call_args_list,
            [call(7, _PLUGIN_ID), call(7, _PLUGIN_ID)],
        )

    # ------------------------------------------------------------ F. 幂等 NotFound
    def test_delete_idempotent_when_not_found(self) -> None:
        """F：get_document 抛 NotFound → 幂等成功，不写 DELETING、不触碰 Milvus/文件。"""
        self.document_repo.get_document.side_effect = (
            DocumentNotFoundError("document not found: id=7")
        )

        async def scenario() -> None:
            await self.service.delete_document(7, plugin_id=_PLUGIN_ID)

        self.run_async(scenario())

        # 幂等成功：不抛异常
        # 不写 DELETING
        self.document_repo.update_status.assert_not_called()
        # 不触碰 Milvus
        self.milvus_repo.query_page_chunks.assert_not_called()
        self.milvus_repo.delete_chunks.assert_not_called()
        # 不触碰文件系统
        self.file_storage.delete.assert_not_called()
        # MySQL 删除不执行
        self.document_repo.delete_document.assert_not_called()

    # ------------------------------------------------------------ G. Protocol 注入
    def test_protocol_injection(self) -> None:
        """G：依赖以 Mock(spec=...) 注入，spec 校验生效。"""
        self.assertIsInstance(self.document_repo, Mock)
        self.assertIsInstance(self.milvus_repo, Mock)
        self.assertIsInstance(self.file_storage, Mock)

        with self.assertRaises(AttributeError):
            _ = self.document_repo.not_a_real_method
        with self.assertRaises(AttributeError):
            _ = self.milvus_repo.not_a_real_method
        with self.assertRaises(AttributeError):
            _ = self.file_storage.not_a_real_method

        self.assertIs(self.service._document_repo, self.document_repo)
        self.assertIs(self.service._milvus_repo, self.milvus_repo)
        self.assertIs(self.service._file_storage, self.file_storage)

    # ================================================================
    # Phase 3.5 Step 2-E：workspace-aware ownership
    # ================================================================
    def test_delete_own_document_success(self) -> None:
        """Step 2-E：A 删除自己的文档 → 完整删除流程，get/delete 均带 plugin_id。"""
        self.document_repo.get_document.return_value = self.fake_document

        async def scenario() -> None:
            await self.service.delete_document(7, plugin_id=_PLUGIN_ID)

        self.run_async(scenario())

        # ownership check + 最终删除均 workspace-aware
        self.document_repo.get_document.assert_called_once_with(7, _PLUGIN_ID)
        self.document_repo.delete_document.assert_called_once_with(7, _PLUGIN_ID)
        # 顺序不变：get → DELETING → delete
        self.assertEqual(
            self.document_repo.mock_calls,
            [
                call.get_document(7, _PLUGIN_ID),
                call.update_status(7, DocumentStatus.DELETING),
                call.delete_document(7, _PLUGIN_ID),
            ],
        )
        # Milvus → FileStorage 顺序不变
        self.assertEqual(
            self.milvus_repo.mock_calls,
            [
                call.query_page_chunks(7),
                call.delete_chunks([{"id": "chunk-1"}, {"id": "chunk-2"}]),
            ],
        )
        self.file_storage.delete.assert_called_once_with("/data/upload.pdf")

    def test_delete_cross_workspace_document_not_found(self) -> None:
        """Step 2-E：A 删除 B（另一插件工作空间）的文档（id=8）→ 按「不存在」幂等处理，不泄露归属。"""
        # A 视角：get_document(8, plugin_id=_PLUGIN_ID) → NotFound（文档 8 属于 B）
        self.document_repo.get_document.side_effect = (
            DocumentNotFoundError("document not found: id=8")
        )

        async def scenario() -> None:
            await self.service.delete_document(8, plugin_id=_PLUGIN_ID)

        self.run_async(scenario())

        # ownership check 以 (8, plugin_id=_PLUGIN_ID) 调用
        self.document_repo.get_document.assert_called_once_with(8, _PLUGIN_ID)
        # 幂等成功：不写 DELETING、不触碰 Milvus/FileStorage/MySQL
        self.document_repo.update_status.assert_not_called()
        self.milvus_repo.query_page_chunks.assert_not_called()
        self.milvus_repo.delete_chunks.assert_not_called()
        self.file_storage.delete.assert_not_called()
        self.document_repo.delete_document.assert_not_called()


class DocumentDeleteRepositoryRegressionTest(unittest.TestCase):
    """
    DocumentDeleteRepository 回归测试（Phase 2.11 Step 2；Phase 3.4 Step C；
    Phase 3.5 Step 2-B plugin-aware 适配）。

    用 SQLite in-memory + StaticPool 验证真实 DB 行为：
    - delete_document 只删目标行、不影响其他文档；
    - update_status 状态机约束（DELETING 前后置）。
    """

    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        # Phase 3.5 Step 2-B：documents.plugin_id FK → plugin_workspaces.plugin_id，
        # 预置一个 Workspace 保持真实 MySQL 语义
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO plugin_workspaces"
                    " (plugin_id, plugin_name, plugin_name_norm, plugin_secret_hash, status)"
                    " VALUES (:pid, :pname, :pnorm, :shash, 'ACTIVE')"
                ),
                {
                    "pid": _PLUGIN_ID,
                    "pname": "Regression Plugin",
                    "pnorm": "regression plugin",
                    "shash": "e" * 64,
                },
            )
        self.repo: DocumentRepositoryImpl = DocumentRepositoryImpl(self.engine)

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_delete_document_only_target_row(self) -> None:
        """回归：delete_document 只删目标行，不影响其他文档（plugin-aware）。"""
        doc_a = self.repo.create_document(
            filename="a.txt", file_path="/data/a.txt", plugin_id=_PLUGIN_ID
        )
        doc_b = self.repo.create_document(
            filename="b.txt", file_path="/data/b.txt", plugin_id=_PLUGIN_ID
        )

        self.repo.delete_document(doc_a.id, plugin_id=_PLUGIN_ID)

        # doc_a 已删除；doc_b 仍在
        with self.assertRaises(DocumentNotFoundError):
            self.repo.get_document(doc_a.id, plugin_id=_PLUGIN_ID)
        fetched_b = self.repo.get_document(doc_b.id, plugin_id=_PLUGIN_ID)
        self.assertEqual(fetched_b.id, doc_b.id)

    def test_update_status_deleting_allowed(self) -> None:
        """回归：update_status(DELETING) 是合法状态（不抛异常）。"""
        doc = self.repo.create_document(
            filename="del.txt", file_path="/data/del.txt", plugin_id=_PLUGIN_ID
        )

        updated = self.repo.update_status(doc.id, DocumentStatus.DELETING)
        self.assertEqual(updated.status, DocumentStatus.DELETING)


if __name__ == "__main__":
    unittest.main()
