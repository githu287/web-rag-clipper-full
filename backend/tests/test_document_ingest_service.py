"""
DocumentIngestService 单元测试（Phase 2.9 Step 2；Phase 2.11 Step 2 扩展；
Phase 3.4 Step C user-aware）。

技术栈：unittest + unittest.mock（不引入 pytest；不依赖真实 MySQL/Milvus/百炼）。
注入方式：Mock(spec=DocumentRepository) + Mock(spec=IngestService)，保证：
  - 通过 Protocol runtime_checkable 校验（H. Protocol 注入）；
  - 断言调用顺序与方法签名，防止实现回归。

Phase 3.4 Step C 变更（user-aware）：
  - ingest_document(document_id, chunks, user_id)：Step 1 ownership check 使用
    get_document(document_id, user_id)；
  - A ingest A：成功，get_document 以 (document_id, user_id) 调用；
  - A ingest B：get_document 抛 DocumentNotFoundError → 直接传播，
    无任何状态写入（不泄露归属）。

覆盖用例（A~H + Phase 2.11 Step 2 扩展 I + Step C user-aware）：
  A. 成功路径：get_document → update_status(PROCESSING, error_message=None)
     → ingest_page → update_ingest_result(chunk_count=len(chunks),
       status=SUCCESS, error_message=None)；
     明确断言不存在单独的 update_status(SUCCESS)。
  B. Embedding/ingest 失败：ingest_page 抛异常 → update_failure(
     error_message=摘要) → 原异常继续抛出 → update_ingest_result 不被调用。
  C. stale-delete 失败：与 B 相同语义（统一走 ingest_page 抛异常）。
  D. get_document NotFound：DocumentNotFoundError 直接传播，
     PROCESSING / FAILED / ingest_page 均不调用。
  E. chunk_count：成功时 update_ingest_result(chunk_count=len(chunks), ...)。
  F. 失败路径 chunk_count：失败只调用 update_failure(document_id,
     error_message=摘要)（原子落 FAILED + error_message），不传 chunk_count，
     不调用 update_ingest_result。
  G. 幂等：同一 document_id 连续调用两次，两次都执行完整成功流程。
  H. Protocol 注入：DocumentRepository / IngestService 均以 Mock(spec=...) 注入。
  I. DELETING gate（Phase 2.11 Step 2）：status == DELETING → 抛
     DocumentOperationError，不调用 update_status(PROCESSING)、不触碰 Milvus、
     不调用 embedding；原状态保持 DELETING。
  J. Step C：A ingest A 成功（get_document 带 user_id）；A ingest B → NotFound。
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from backend.core.exceptions import (
    DocumentNotFoundError,
    DocumentOperationError,
)
from backend.models.document import DocumentStatus
from backend.repositories.mysql import DocumentRepository
from backend.services.document_ingest import DocumentIngestService
from backend.services.ingest import IngestService


class DocumentIngestServiceTest(unittest.TestCase):
    """DocumentIngestService 单元测试（unittest + mock）。"""

    def setUp(self) -> None:
        """构造注入依赖：Mock(spec=Protocol)，符合 H. Protocol 注入。"""
        self.document_repo = Mock(spec=DocumentRepository)
        self.ingest_service = Mock(spec=IngestService)

        # 默认返回一个假 Document 对象（存在性校验用）
        self.fake_document = Mock()
        self.fake_document.id = 1
        self.fake_document.status = DocumentStatus.PENDING
        self.fake_document.chunk_count = 0
        self.document_repo.get_document.return_value = self.fake_document

        # ingest_page 是 async 方法，默认成功
        # （外层已 Mock(spec=IngestService)，此处直接 AsyncMock() 保证 await 语义）
        self.ingest_service.ingest_page = AsyncMock()
        self.ingest_service.ingest_page.return_value = None

        self.service = DocumentIngestService(
            document_repository=self.document_repo,
            ingest_service=self.ingest_service,
        )

    def run_async(self, coro) -> object:
        """同步运行 async 测试体。"""
        return asyncio.run(coro)

    # ------------------------------------------------------------------ A. 成功路径
    def test_success_path_call_order(self) -> None:
        """A：成功路径严格按 get→PROCESSING→ingest_page→update_ingest_result 顺序。"""

        async def scenario() -> None:
            await self.service.ingest_document(1, ["c1", "c2"], user_id=1)

        self.run_async(scenario())

        # 1) 调用顺序（Step C：get_document 带 user_id=1）
        self.assertEqual(
            self.document_repo.mock_calls,
            [
                unittest.mock.call.get_document(1, 1),
                unittest.mock.call.update_status(
                    1, DocumentStatus.PROCESSING, error_message=None
                ),
                unittest.mock.call.update_ingest_result(
                    1,
                    chunk_count=2,
                    status=DocumentStatus.SUCCESS,
                    error_message=None,
                ),
            ],
        )
        # 2) ingest_page 以 page_id=document_id 调用（Phase 3.4 Step 4/F6：user_id + api_key 透传）
        self.ingest_service.ingest_page.assert_called_once_with(
            page_id=1,
            chunks=["c1", "c2"],
            user_id=1,
            api_key=None,
        )

    def test_success_path_no_extra_update_status_success(self) -> None:
        """A：明确断言不允许单独出现 update_status(SUCCESS)。"""
        async def scenario() -> None:
            await self.service.ingest_document(1, ["c1"], user_id=1)

        self.run_async(scenario())

        # 遍历所有调用，update_status 只允许出现 PROCESSING 一次，绝无 SUCCESS
        status_calls = [
            c for c in self.document_repo.mock_calls
            if c[0] == "update_status"
        ]
        self.assertEqual(len(status_calls), 1)
        self.assertEqual(
            status_calls[0],
            unittest.mock.call.update_status(
                1, DocumentStatus.PROCESSING, error_message=None
            ),
        )
        # SUCCESS 必须通过 update_ingest_result 完成（并清空 error_message）
        self.document_repo.update_ingest_result.assert_called_once_with(
            1,
            chunk_count=1,
            status=DocumentStatus.SUCCESS,
            error_message=None,
        )

    # ------------------------------------------------------- B. Embedding/ingest 失败
    def test_ingest_failure_marks_failed_and_re_raises(self) -> None:
        """B：ingest_page 抛异常 → FAILED + 原异常继续抛出 + 不调用 update_ingest_result。"""
        original_error = RuntimeError("embedding service down")

        async def scenario() -> None:
            self.ingest_service.ingest_page.side_effect = original_error
            with self.assertRaises(RuntimeError) as cm:
                await self.service.ingest_document(1, ["c1"], user_id=1)
            self.assertIs(cm.exception, original_error)

        self.run_async(scenario())

        # FAILED + error_message 已通过 update_failure 原子落库
        self.document_repo.update_failure.assert_any_call(
            1, error_message="embedding service down"
        )
        # update_ingest_result 不被调用
        self.document_repo.update_ingest_result.assert_not_called()

    def test_failure_path_no_chunk_count_in_update_status(self) -> None:
        """F：失败路径只调用 update_failure(FAILED + error_message)，不传 chunk_count。"""
        async def scenario() -> None:
            self.ingest_service.ingest_page.side_effect = RuntimeError("fail")
            with self.assertRaises(RuntimeError):
                await self.service.ingest_document(1, ["c1"], user_id=1)

        self.run_async(scenario())

        # FAILED 通过 update_failure 原子落库（document_id + error_message），
        # 绝不传 chunk_count（update_failure 签名不含 chunk_count）
        self.document_repo.update_failure.assert_called_once_with(
            1, error_message="fail"
        )
        # update_status 只出现 PROCESSING（带 error_message=None），无 FAILED
        status_calls = [
            c for c in self.document_repo.mock_calls
            if c[0] == "update_status"
        ]
        self.assertEqual(len(status_calls), 1)
        self.assertEqual(
            status_calls[0][1], (1, DocumentStatus.PROCESSING)
        )
        # 所有 update_status 调用均不带 chunk_count
        for c in status_calls:
            self.assertNotIn("chunk_count", c[2])
        # update_ingest_result 不被调用
        self.document_repo.update_ingest_result.assert_not_called()

    # ---------------------------------------------------------- C. stale-delete 失败
    def test_stale_delete_failure_marks_failed(self) -> None:
        """C：stale-delete 失败（ingest_page 抛 Milvus 异常）→ FAILED + 原异常抛出。"""
        class FakeMilvusError(Exception):
            pass

        milvus_error = FakeMilvusError("delete_chunks failed")

        async def scenario() -> None:
            self.ingest_service.ingest_page.side_effect = milvus_error
            with self.assertRaises(FakeMilvusError) as cm:
                await self.service.ingest_document(1, ["c1"], user_id=1)
            self.assertIs(cm.exception, milvus_error)

        self.run_async(scenario())

        self.document_repo.update_failure.assert_any_call(
            1, error_message="delete_chunks failed"
        )
        self.document_repo.update_ingest_result.assert_not_called()

    # -------------------------------------------------- D. get_document NotFound
    def test_document_not_found_no_status_write(self) -> None:
        """D：get_document 抛 DocumentNotFoundError → 直接传播，不做任何状态写入。"""
        async def scenario() -> None:
            self.document_repo.get_document.side_effect = (
                DocumentNotFoundError("document not found: id=999")
            )
            with self.assertRaises(DocumentNotFoundError):
                await self.service.ingest_document(999, ["c1"], user_id=1)

        self.run_async(scenario())

        # PROCESSING / FAILED 均不调用
        self.document_repo.update_status.assert_not_called()
        self.document_repo.update_failure.assert_not_called()
        # ingest_page 不调用
        self.ingest_service.ingest_page.assert_not_called()
        # update_ingest_result 不调用
        self.document_repo.update_ingest_result.assert_not_called()

    # ------------------------------------------------------------ E. chunk_count
    def test_success_chunk_count_matches_len(self) -> None:
        """E：成功时 update_ingest_result(chunk_count=len(chunks))。"""
        chunks = ["c1", "c2", "c3"]

        async def scenario() -> None:
            await self.service.ingest_document(7, chunks, user_id=1)

        self.run_async(scenario())

        self.document_repo.update_ingest_result.assert_called_once_with(
            7,
            chunk_count=3,
            status=DocumentStatus.SUCCESS,
            error_message=None,
        )

    # ---------------------------------------------------------------- G. 幂等
    def test_idempotent_double_ingest(self) -> None:
        """G：同一 document_id 连续调用两次，两次均完整走成功流程。"""
        async def scenario() -> None:
            await self.service.ingest_document(1, ["c1", "c2"], user_id=1)
            await self.service.ingest_document(1, ["c1", "c2"], user_id=1)

        self.run_async(scenario())

        # get_document 被调用两次（每次均带 user_id）
        self.assertEqual(
            self.document_repo.get_document.call_count, 2
        )
        self.assertEqual(
            self.document_repo.get_document.call_args_list,
            [unittest.mock.call(1, 1), unittest.mock.call(1, 1)],
        )
        # PROCESSING 两次
        processing_calls = [
            c for c in self.document_repo.mock_calls
            if c[0] == "update_status"
            and c[1] == (1, DocumentStatus.PROCESSING)
        ]
        self.assertEqual(len(processing_calls), 2)
        # ingest_page 两次（page_id 始终等于 document_id）
        self.assertEqual(self.ingest_service.ingest_page.call_count, 2)
        for call in self.ingest_service.ingest_page.call_args_list:
            self.assertEqual(
                call,
                unittest.mock.call(
                    page_id=1, chunks=["c1", "c2"], user_id=1, api_key=None
                ),
            )
        # update_ingest_result 两次，chunk_count=2, status=SUCCESS
        self.assertEqual(
            self.document_repo.update_ingest_result.call_count, 2
        )
        for call in self.document_repo.update_ingest_result.call_args_list:
            self.assertEqual(
                call,
                unittest.mock.call(
                    1,
                    chunk_count=2,
                    status=DocumentStatus.SUCCESS,
                    error_message=None,
                ),
            )

    # ------------------------------------------------ I. update_ingest_result 失败
    def test_update_ingest_result_failure(self) -> None:
        """
        I：Milvus 已成功、MySQL SUCCESS 落库失败时的行为。

        当前实现：update_ingest_result() 位于 try/except 之外，
        异常直接向上传播 —— 不进入 except、不调用 update_status(FAILED)、
        不修改 chunk_count、最终抛出的仍是原始异常。
        这与最终方案一致（Milvus 成功后 MySQL 更新失败 → 保持 PROCESSING，
        由下次 ingest 重入收敛）。
        """
        original_error = RuntimeError("mysql commit failed")

        async def scenario() -> None:
            self.document_repo.update_ingest_result.side_effect = original_error
            with self.assertRaises(RuntimeError) as cm:
                await self.service.ingest_document(1, ["c1"], user_id=1)
            self.assertIs(cm.exception, original_error)

        self.run_async(scenario())

        # 1) 调用顺序：get(带 user_id) → PROCESSING → ingest_page → update_ingest_result(失败)
        self.assertEqual(
            self.document_repo.mock_calls,
            [
                unittest.mock.call.get_document(1, 1),
                unittest.mock.call.update_status(
                    1, DocumentStatus.PROCESSING, error_message=None
                ),
                unittest.mock.call.update_ingest_result(
                    1,
                    chunk_count=1,
                    status=DocumentStatus.SUCCESS,
                    error_message=None,
                ),
            ],
        )
        self.ingest_service.ingest_page.assert_called_once_with(
            page_id=1,
            chunks=["c1"],
            user_id=1,
            api_key=None,
        )

        # 2) FAILED 不被调用（异常在 try/except 之外，不进入失败路径）
        self.document_repo.update_failure.assert_not_called()
        failed_calls = [
            c for c in self.document_repo.mock_calls
            if c[0] == "update_status"
            and c[1][1] == DocumentStatus.FAILED
        ]
        self.assertEqual(failed_calls, [])

        # 3) SUCCESS 不被单独调用（update_status 只出现 PROCESSING 一次）
        success_calls = [
            c for c in self.document_repo.mock_calls
            if c[0] == "update_status"
            and c[1][1] == DocumentStatus.SUCCESS
        ]
        self.assertEqual(success_calls, [])

        # 4) 所有 update_status 调用均不带 chunk_count（chunk_count 未被额外修改）
        for c in self.document_repo.mock_calls:
            if c[0] == "update_status":
                self.assertNotIn("chunk_count", c[2])

    # ------------------------------------------- I. DELETING gate（Phase 2.11 Step 2）
    def test_ingest_rejected_when_deleting(self) -> None:
        """I：status == DELETING → 拒绝，不写 PROCESSING、不触碰 Milvus、不 embedding。"""
        self.fake_document.status = DocumentStatus.DELETING

        async def scenario() -> None:
            with self.assertRaises(DocumentOperationError):
                await self.service.ingest_document(1, ["c1"], user_id=1)

        self.run_async(scenario())

        # 不调用 update_status（不写 PROCESSING，原状态保持 DELETING）
        self.document_repo.update_status.assert_not_called()
        # 不触碰 Milvus（ingest_page 不调用 → 不 query/upsert/embedding）
        self.ingest_service.ingest_page.assert_not_called()
        # 不落 SUCCESS / FAILED
        self.document_repo.update_ingest_result.assert_not_called()
        self.document_repo.update_failure.assert_not_called()

    # -------------------------------------------------------- H. Protocol 注入
    def test_protocol_injection(self) -> None:
        """H：依赖以 Mock(spec=DocumentRepository) / Mock(spec=IngestService) 注入。"""
        # setUp 已用 Mock(spec=...) 构造；这里验证 isinstance 与 spec 校验生效
        self.assertIsInstance(self.document_repo, Mock)
        self.assertIsInstance(self.ingest_service, Mock)

        # 访问 Protocol 不存在的方法应抛 AttributeError（spec 生效）
        with self.assertRaises(AttributeError):
            _ = self.document_repo.not_a_real_method
        with self.assertRaises(AttributeError):
            _ = self.ingest_service.not_a_real_method

        # 服务对象已注入两个依赖
        self.assertIs(self.service._document_repo, self.document_repo)
        self.assertIs(self.service._ingest_service, self.ingest_service)

    # ================================================================
    # Phase 3.4 Step C：user-aware ownership
    # ================================================================
    def test_ingest_own_document_with_user_id(self) -> None:
        """Step C：A(1) ingest 自己的文档 → 成功，get_document 以 (1, 1) 调用。"""
        async def scenario() -> None:
            await self.service.ingest_document(1, ["c1", "c2"], user_id=1)

        self.run_async(scenario())

        # ownership check：get_document(document_id, user_id)
        self.document_repo.get_document.assert_called_once_with(1, 1)
        # 成功流程照常（PROCESSING → ingest_page → SUCCESS）
        self.document_repo.update_status.assert_called_once_with(
            1, DocumentStatus.PROCESSING, error_message=None
        )
        self.document_repo.update_ingest_result.assert_called_once_with(
            1,
            chunk_count=2,
            status=DocumentStatus.SUCCESS,
            error_message=None,
        )
        self.document_repo.update_failure.assert_not_called()

    def test_ingest_cross_user_not_found(self) -> None:
        """Step C：A(1) ingest B(2) 的文档（id=8）→ NotFound，无任何状态写入。"""
        async def scenario() -> None:
            self.document_repo.get_document.side_effect = DocumentNotFoundError(
                "document not found: id=8"
            )
            with self.assertRaises(DocumentNotFoundError):
                await self.service.ingest_document(8, ["c1"], user_id=1)

        self.run_async(scenario())

        # ownership check 以 (8, user_id=1) 调用（A 视角文档 8 不存在）
        self.document_repo.get_document.assert_called_once_with(8, 1)
        # 不写 PROCESSING / FAILED / SUCCESS、不触碰 Milvus
        self.document_repo.update_status.assert_not_called()
        self.document_repo.update_failure.assert_not_called()
        self.document_repo.update_ingest_result.assert_not_called()
        self.ingest_service.ingest_page.assert_not_called()

    # ================================================================
    # Phase 3.4 Step F6：user_id / api_key 透传
    # ================================================================
    def test_api_key_and_user_id_passed_to_ingest_page(self) -> None:
        """F6：ingest_document 将 user_id 与 api_key 原样透传给 ingest_page。"""
        async def scenario() -> None:
            await self.service.ingest_document(
                1, ["c1", "c2"], user_id=7, api_key="sk-user"
            )

        self.run_async(scenario())

        # ownership 以 (document_id, user_id=7) 校验
        self.document_repo.get_document.assert_called_once_with(1, 7)
        # ingest_page 收到 user_id=7 + api_key="sk-user"（透传用户自己的 Key）
        self.ingest_service.ingest_page.assert_called_once_with(
            page_id=1,
            chunks=["c1", "c2"],
            user_id=7,
            api_key="sk-user",
        )


if __name__ == "__main__":
    unittest.main()
