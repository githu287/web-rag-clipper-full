"""
RagService Document status post-filter 单元测试（Phase 2.12 Step 2；
Phase 2.13 Step 2 增 Document metadata 组装测试）。

覆盖用例（对应 Phase 2.12 Step 2 验收清单 A~L，类 RagServiceStatusFilterTest）：
  A. SUCCESS 放行：page_id=1、2 均 SUCCESS → 全部保留。
  B. FAILED 过滤：2 为 FAILED → 只保留 1。
  C. DELETING 过滤：2 为 DELETING → 只保留 1。
  D. PROCESSING 过滤：2 为 PROCESSING → 只保留 1。
  E. PENDING 过滤：2 为 PENDING → 只保留 1。
  F. Orphan chunk 过滤：999 在 MySQL 不存在 → 只返回 1。
  G. limit=1：Milvus search(limit=10)，最终返回 ≤1 条。
  H. limit=10：Milvus search(limit=10)，最终返回 ≤10 条。
  I. limit=20：Milvus search(limit=20)，最终返回 ≤20 条。
  J. 空 Milvus 候选：get_documents_by_ids 不被调用，返回 []。
  K. DocumentRepository 异常：DocumentOperationError 原样向上传播（不伪装为空结果）。
  L. page_id 去重：page_id=[1,1,1,2] 去重为 [1,2]，get_documents_by_ids 只调用一次。

Phase 2.13 Step 2 metadata 覆盖（类 RagServiceMetadataTest，A~L）：
  A. SUCCESS chunk 附带完整 metadata（document_id/filename/status/created_at）。
  B. filename 正确（来自 document_map）。
  C. status 正确（SUCCESS）。
  D. created_at 正确（透传 Document.created_at）。
  E. document_id == page_id（1:1）。
  F. orphan chunk 仍然被过滤（metadata 不改变过滤行为）。
  G. FAILED/PROCESSING/PENDING/DELETING 仍然被过滤。
  H. 多个 chunk 属于同一 document 时只调用一次 get_documents_by_ids（无 N+1）。
  I. DocumentRepository 返回顺序打乱时 metadata 仍按 ID 正确匹配（禁止依赖顺序）。
  J. limit=20 行为不改变（仍 search(limit=20)）。
  K. 空候选不触发 DocumentRepository。
  L. DocumentRepository 异常继续向上传播。

Phase 3.1 Step 3 metadata 覆盖（类 RagServiceMetadataTest，M~P）：
  M. webpage 剪藏文档的 title/url/source_type 正确返回。
  N. 上传文档无 title/url，source_type=upload 正确返回（默认值）。
  O. Repository 返回顺序打乱时 title/url/source_type 仍按 document_map 正确匹配。
  P. orphan filter / SUCCESS-only filter 不变（metadata 不改变过滤行为）。

技术栈：unittest + unittest.mock（Mock(spec=...)），禁止真实连接
MySQL / Milvus / 百炼。
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from unittest.mock import Mock

from backend.clients.embedding import EmbeddingClient
from backend.core.exceptions import (
    DocumentNotFoundError,
    DocumentNotSuccessError,
    DocumentOperationError,
)
from backend.models.api_schema import RagSearchResult
from backend.models.document import Document, DocumentStatus
from backend.models.milvus_dto import ChunkSearchResult
from backend.repositories.milvus import MilvusRepository
from backend.repositories.mysql import DocumentRepository
from backend.services.rag import RagService

_QUERY_VECTOR: list[float] = [0.1] * 1024


class RagServiceStatusFilterTest(unittest.TestCase):
    """RagService status post-filter 单元测试（Mock 三个依赖，不连真实服务）。"""

    def setUp(self) -> None:
        self.embedding = Mock(spec=EmbeddingClient)
        self.embedding.embed.return_value = [_QUERY_VECTOR]
        self.milvus = Mock(spec=MilvusRepository)
        self.documents = Mock(spec=DocumentRepository)
        # Phase 3.5 Step 2-E：全库模式先取当前插件工作空间 SUCCESS document ids
        # （单测默认 plugin-a 有 doc 1、2；特殊用例按需覆盖）。
        self.documents.get_success_document_ids.return_value = [1, 2]
        self.service = RagService(
            embedding_client=self.embedding,
            milvus_repository=self.milvus,
            document_repository=self.documents,
        )

    # ------------------------------------------------------------------ helpers
    def _chunk(self, pk: str, page_id: int, index: int = 0) -> ChunkSearchResult:
        return ChunkSearchResult(
            id=pk,
            page_id=page_id,
            chunk_index=index,
            chunk_text=f"text-{pk}",
            distance=0.1,
        )

    def _doc(self, doc_id: int, status: str) -> Document:
        return Document(
            id=doc_id,
            filename=f"file-{doc_id}.txt",
            file_path=f"uploads/file-{doc_id}.txt",
            status=status,
        )

    def _search(self, query: str = "hello", limit: int = 5) -> list[ChunkSearchResult]:
        # Phase 3.5 Step 2-E：全库模式要求 plugin_id（匿名返回 []）。
        return asyncio.run(self.service.search(query=query, limit=limit, plugin_id="plugin-a"))

    # ------------------------------------------------------------- A. SUCCESS 放行
    def test_a_success_passthrough(self) -> None:
        candidates = [self._chunk("1_0", 1), self._chunk("2_0", 2)]
        self.milvus.search.return_value = candidates
        self.documents.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS),
            self._doc(2, DocumentStatus.SUCCESS),
        ]

        results = self._search()

        self.assertEqual([c.id for c in results], ["1_0", "2_0"])
        self.documents.get_documents_by_ids.assert_called_once_with([1, 2], "plugin-a")

    # ------------------------------------------------------------- B. FAILED 过滤
    def test_b_failed_filtered(self) -> None:
        candidates = [self._chunk("1_0", 1), self._chunk("2_0", 2)]
        self.milvus.search.return_value = candidates
        self.documents.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS),
            self._doc(2, DocumentStatus.FAILED),
        ]

        results = self._search()

        self.assertEqual([c.id for c in results], ["1_0"])

    # ----------------------------------------------------------- C. DELETING 过滤
    def test_c_deleting_filtered(self) -> None:
        candidates = [self._chunk("1_0", 1), self._chunk("2_0", 2)]
        self.milvus.search.return_value = candidates
        self.documents.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS),
            self._doc(2, DocumentStatus.DELETING),
        ]

        results = self._search()

        self.assertEqual([c.id for c in results], ["1_0"])

    # --------------------------------------------------------- D. PROCESSING 过滤
    def test_d_processing_filtered(self) -> None:
        candidates = [self._chunk("1_0", 1), self._chunk("2_0", 2)]
        self.milvus.search.return_value = candidates
        self.documents.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS),
            self._doc(2, DocumentStatus.PROCESSING),
        ]

        results = self._search()

        self.assertEqual([c.id for c in results], ["1_0"])

    # ------------------------------------------------------------ E. PENDING 过滤
    def test_e_pending_filtered(self) -> None:
        candidates = [self._chunk("1_0", 1), self._chunk("2_0", 2)]
        self.milvus.search.return_value = candidates
        self.documents.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS),
            self._doc(2, DocumentStatus.PENDING),
        ]

        results = self._search()

        self.assertEqual([c.id for c in results], ["1_0"])

    # ------------------------------------------------------ F. Orphan chunk 过滤
    def test_f_orphan_chunk_filtered(self) -> None:
        candidates = [self._chunk("1_0", 1), self._chunk("999_0", 999)]
        self.milvus.search.return_value = candidates
        # 999 属于当前插件工作空间 SUCCESS 集合（否则会被隔离兜底提前剔除，
        # 无法单独验证 orphan 过滤）；MySQL 层只有 id=1，999 → 孤儿被过滤。
        self.documents.get_success_document_ids.return_value = [1, 999]
        # MySQL 只有 id=1；999 不存在 → 应被当作孤儿过滤。
        self.documents.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS),
        ]

        results = self._search()

        self.assertEqual([c.id for c in results], ["1_0"])
        self.documents.get_documents_by_ids.assert_called_once_with([1, 999], "plugin-a")

    # ---------------------------------------------------------------- G. limit=1
    def test_g_limit_1_search_10(self) -> None:
        candidates = [self._chunk("1_0", 1), self._chunk("2_0", 2)]
        self.milvus.search.return_value = candidates
        self.documents.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS),
            self._doc(2, DocumentStatus.SUCCESS),
        ]

        results = self._search(limit=1)

        # Step 2-E：全库模式 Milvus 检索携带 Workspace 隔离 expr（success_ids=[1, 2]）
        self.milvus.search.assert_called_once_with(
            _QUERY_VECTOR, limit=10, expr="page_id in [1,2]"
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, "1_0")

    # --------------------------------------------------------------- H. limit=10
    def test_h_limit_10_search_10(self) -> None:
        candidates = [self._chunk(f"{i}_0", i) for i in range(1, 11)]
        self.milvus.search.return_value = candidates
        # 当前插件工作空间 SUCCESS 集合覆盖全部候选 page_id（避免隔离兜底误过滤）
        self.documents.get_success_document_ids.return_value = list(range(1, 11))
        self.documents.get_documents_by_ids.return_value = [
            self._doc(i, DocumentStatus.SUCCESS) for i in range(1, 11)
        ]

        results = self._search(limit=10)

        # Step 2-E：expr 覆盖当前插件工作空间全部 SUCCESS ids（1..10）
        self.milvus.search.assert_called_once_with(
            _QUERY_VECTOR,
            limit=10,
            expr="page_id in [1,2,3,4,5,6,7,8,9,10]",
        )
        self.assertEqual(len(results), 10)

    # --------------------------------------------------------------- I. limit=20
    def test_i_limit_20_search_20(self) -> None:
        candidates = [self._chunk(f"{i}_0", i) for i in range(1, 21)]
        self.milvus.search.return_value = candidates
        # 当前插件工作空间 SUCCESS 集合覆盖全部候选 page_id（避免隔离兜底误过滤）
        self.documents.get_success_document_ids.return_value = list(range(1, 21))
        self.documents.get_documents_by_ids.return_value = [
            self._doc(i, DocumentStatus.SUCCESS) for i in range(1, 21)
        ]

        results = self._search(limit=20)

        # Step 2-E：expr 覆盖当前插件工作空间全部 SUCCESS ids（1..20）
        self.milvus.search.assert_called_once_with(
            _QUERY_VECTOR,
            limit=20,
            expr=(
                "page_id in [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20]"
            ),
        )
        self.assertEqual(len(results), 20)

    # --------------------------------------------------------- J. 空 Milvus 候选
    def test_j_empty_candidates_skips_repository(self) -> None:
        self.milvus.search.return_value = []

        results = self._search()

        self.assertEqual(results, [])
        self.documents.get_documents_by_ids.assert_not_called()

    # ------------------------------------------------- K. DocumentRepository 异常
    def test_k_repository_error_propagates(self) -> None:
        candidates = [self._chunk("1_0", 1)]
        self.milvus.search.return_value = candidates
        self.documents.get_documents_by_ids.side_effect = DocumentOperationError(
            "get_documents_by_ids failed: db down"
        )

        with self.assertRaises(DocumentOperationError):
            self._search()

    # --------------------------------------------------------- L. page_id 去重
    def test_l_page_id_deduplicated(self) -> None:
        candidates = [
            self._chunk("1_0", 1, index=0),
            self._chunk("1_1", 1, index=1),
            self._chunk("1_2", 1, index=2),
            self._chunk("2_0", 2, index=0),
        ]
        self.milvus.search.return_value = candidates
        self.documents.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS),
            self._doc(2, DocumentStatus.SUCCESS),
        ]

        results = self._search()

        # 去重后 [1, 2]，只调用一次；不能传 [1, 1, 1, 2]。
        self.documents.get_documents_by_ids.assert_called_once_with([1, 2], "plugin-a")
        self.assertEqual([c.id for c in results], ["1_0", "1_1", "1_2", "2_0"])


class RagServiceMetadataTest(unittest.TestCase):
    """
    RagService Document metadata 组装测试（Phase 2.13 Step 2，验收清单 A~L）。

    重点正确性点：
        - document_id == page_id（1:1）；
        - metadata 通过 document_map[id] 关联，禁止依赖 get_documents_by_ids 返回顺序；
        - 过滤逻辑 / 查询次数与 Phase 2.12 完全一致（无 N+1）。
    """

    def setUp(self) -> None:
        self.embedding = Mock(spec=EmbeddingClient)
        self.embedding.embed.return_value = [_QUERY_VECTOR]
        self.milvus = Mock(spec=MilvusRepository)
        self.documents = Mock(spec=DocumentRepository)
        # 全库模式先取当前 Plugin 的 SUCCESS document ids
        self.documents.get_success_document_ids.return_value = [1, 2]
        self.service = RagService(
            embedding_client=self.embedding,
            milvus_repository=self.milvus,
            document_repository=self.documents,
        )

    # ------------------------------------------------------------------ helpers
    def _chunk(self, pk: str, page_id: int, index: int = 0) -> ChunkSearchResult:
        return ChunkSearchResult(
            id=pk,
            page_id=page_id,
            chunk_index=index,
            chunk_text=f"text-{pk}",
            distance=0.1,
        )

    def _doc(
        self,
        doc_id: int,
        status: str,
        created_at: datetime | None = None,
        title: str | None = None,
        url: str | None = None,
        source_type: str | None = None,
    ) -> Document:
        return Document(
            id=doc_id,
            filename=f"file-{doc_id}.txt",
            file_path=f"uploads/file-{doc_id}.txt",
            status=status,
            created_at=created_at or datetime(2026, 1, 1, 12, 0, 0),
            title=title,
            url=url,
            source_type=source_type,
        )

    def _search(
        self,
        query: str = "hello",
        limit: int = 5,
    ) -> list[RagSearchResult]:
        # Phase 3.5 Step 2-E：全库模式要求 plugin_id（匿名返回 []）。
        return asyncio.run(self.service.search(query=query, limit=limit, plugin_id="plugin-a"))

    # ------------------------------------- A. SUCCESS chunk 附带完整 metadata
    def test_a_success_chunk_attaches_metadata(self) -> None:
        candidates = [self._chunk("1_0", 1)]
        self.milvus.search.return_value = candidates
        self.documents.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS),
        ]

        results = self._search()

        self.assertEqual(len(results), 1)
        result = results[0]
        # 原有 5 个检索字段全部保留
        self.assertEqual(result.id, "1_0")
        self.assertEqual(result.page_id, 1)
        self.assertEqual(result.chunk_index, 0)
        self.assertEqual(result.chunk_text, "text-1_0")
        self.assertEqual(result.distance, 0.1)
        # 新增 4 个 metadata 字段
        self.assertEqual(result.document_id, 1)
        self.assertEqual(result.filename, "file-1.txt")
        self.assertEqual(result.status, DocumentStatus.SUCCESS)
        self.assertIsNotNone(result.created_at)

    # -------------------------------------------------- B. filename 正确
    def test_b_filename_correct(self) -> None:
        candidates = [self._chunk("1_0", 1)]
        self.milvus.search.return_value = candidates
        self.documents.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS),
        ]

        results = self._search()

        self.assertEqual(results[0].filename, "file-1.txt")

    # ----------------------------------------------------- C. status 正确
    def test_c_status_correct(self) -> None:
        candidates = [self._chunk("1_0", 1)]
        self.milvus.search.return_value = candidates
        self.documents.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS),
        ]

        results = self._search()

        self.assertEqual(results[0].status, DocumentStatus.SUCCESS)

    # --------------------------------------------------- D. created_at 正确
    def test_d_created_at_correct(self) -> None:
        created_at = datetime(2026, 8, 23, 10, 30, 0)
        candidates = [self._chunk("1_0", 1)]
        self.milvus.search.return_value = candidates
        self.documents.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS, created_at=created_at),
        ]

        results = self._search()

        self.assertEqual(results[0].created_at, created_at)

    # ------------------------------------------- E. document_id == page_id
    def test_e_document_id_equals_page_id(self) -> None:
        candidates = [self._chunk("7_0", 7)]
        self.milvus.search.return_value = candidates
        # page 7 属于当前插件工作空间 SUCCESS 集合（避免隔离兜底误过滤）
        self.documents.get_success_document_ids.return_value = [7]
        self.documents.get_documents_by_ids.return_value = [
            self._doc(7, DocumentStatus.SUCCESS),
        ]

        results = self._search()

        self.assertEqual(results[0].document_id, 7)
        self.assertEqual(results[0].document_id, results[0].page_id)

    # ---------------------------------------- F. orphan chunk 仍然被过滤
    def test_f_orphan_chunk_still_filtered(self) -> None:
        candidates = [self._chunk("1_0", 1), self._chunk("999_0", 999)]
        self.milvus.search.return_value = candidates
        self.documents.get_success_document_ids.return_value = [1, 999]
        self.documents.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS),
        ]

        results = self._search()

        self.assertEqual([r.id for r in results], ["1_0"])
        self.documents.get_documents_by_ids.assert_called_once_with([1, 999], "plugin-a")

    # ------------------------------ G. 非 SUCCESS 状态仍然被过滤
    def test_g_non_success_statuses_still_filtered(self) -> None:
        candidates = [self._chunk(f"{i}_0", i) for i in range(1, 6)]
        self.documents.get_success_document_ids.return_value = list(range(1, 6))
        self.milvus.search.return_value = candidates
        self.documents.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS),
            self._doc(2, DocumentStatus.FAILED),
            self._doc(3, DocumentStatus.PROCESSING),
            self._doc(4, DocumentStatus.PENDING),
            self._doc(5, DocumentStatus.DELETING),
        ]

        results = self._search()

        self.assertEqual([r.id for r in results], ["1_0"])

    # ------------------------- H. 多 chunk 同 document 只查询一次（无 N+1）
    def test_h_multiple_chunks_single_query(self) -> None:
        candidates = [
            self._chunk("1_0", 1, index=0),
            self._chunk("1_1", 1, index=1),
            self._chunk("1_2", 1, index=2),
        ]
        self.milvus.search.return_value = candidates
        self.documents.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS),
        ]

        results = self._search()

        self.documents.get_documents_by_ids.assert_called_once_with([1], "plugin-a")
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.document_id == 1 for r in results))
        self.assertEqual({r.filename for r in results}, {"file-1.txt"})

    # ---------------- I. Repository 返回顺序打乱时 metadata 仍正确匹配（重点）
    def test_i_metadata_matching_when_repository_order_scrambled(self) -> None:
        candidates = [self._chunk("1_0", 1), self._chunk("2_0", 2)]
        self.milvus.search.return_value = candidates
        # get_documents_by_ids 返回 [doc2, doc1]，与 page_ids=[1,2] 顺序相反
        self.documents.get_documents_by_ids.return_value = [
            self._doc(2, DocumentStatus.SUCCESS),
            self._doc(1, DocumentStatus.SUCCESS),
        ]

        results = self._search()

        self.assertEqual(len(results), 2)
        # candidate page_id=1 必须拿到 doc1（不是 doc2）
        self.assertEqual(results[0].page_id, 1)
        self.assertEqual(results[0].document_id, 1)
        self.assertEqual(results[0].filename, "file-1.txt")
        self.assertEqual(results[0].status, DocumentStatus.SUCCESS)
        # candidate page_id=2 必须拿到 doc2
        self.assertEqual(results[1].page_id, 2)
        self.assertEqual(results[1].document_id, 2)
        self.assertEqual(results[1].filename, "file-2.txt")
        self.assertEqual(results[1].status, DocumentStatus.SUCCESS)

    # ------------------------------------------ J. limit=20 行为不改变
    def test_j_limit_20_unchanged(self) -> None:
        candidates = [self._chunk(f"{i}_0", i) for i in range(1, 21)]
        self.milvus.search.return_value = candidates
        # 当前插件工作空间 SUCCESS 集合覆盖全部候选 page_id（避免隔离兜底误过滤）
        self.documents.get_success_document_ids.return_value = list(range(1, 21))
        self.documents.get_documents_by_ids.return_value = [
            self._doc(i, DocumentStatus.SUCCESS) for i in range(1, 21)
        ]

        results = self._search(limit=20)

        # Step 2-E：expr 覆盖当前插件工作空间全部 SUCCESS ids（1..20）
        self.milvus.search.assert_called_once_with(
            _QUERY_VECTOR,
            limit=20,
            expr=(
                "page_id in [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20]"
            ),
        )
        self.assertEqual(len(results), 20)
        self.assertTrue(all(r.document_id == r.page_id for r in results))

    # ---------------------------------- K. 空候选不触发 DocumentRepository
    def test_k_empty_candidates_skips_repository(self) -> None:
        self.milvus.search.return_value = []

        results = self._search()

        self.assertEqual(results, [])
        self.documents.get_documents_by_ids.assert_not_called()

    # ------------------------------- L. DocumentRepository 异常向上传播
    def test_l_repository_error_propagates(self) -> None:
        candidates = [self._chunk("1_0", 1)]
        self.milvus.search.return_value = candidates
        self.documents.get_documents_by_ids.side_effect = DocumentOperationError(
            "get_documents_by_ids failed: db down"
        )

        with self.assertRaises(DocumentOperationError):
            self._search()

    # ----------------- Phase 3.1 Step 3：网页剪藏来源元数据（title/url/source_type）
    def test_m_webclip_metadata_returned(self) -> None:
        """M：webpage 剪藏文档的 title/url/source_type 正确返回。"""
        candidates = [self._chunk("1_0", 1)]
        self.milvus.search.return_value = candidates
        self.documents.get_documents_by_ids.return_value = [
            self._doc(
                1,
                DocumentStatus.SUCCESS,
                title="示例文章",
                url="https://example.com/article/1",
                source_type="webpage",
            ),
        ]

        results = self._search()

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.title, "示例文章")
        self.assertEqual(result.url, "https://example.com/article/1")
        self.assertEqual(result.source_type, "webpage")
        # 原有 metadata 不回归
        self.assertEqual(result.document_id, 1)
        self.assertEqual(result.filename, "file-1.txt")
        self.assertEqual(result.status, DocumentStatus.SUCCESS)

    def test_n_upload_document_metadata_defaults(self) -> None:
        """N：上传文档无 title/url，source_type=upload 正确返回。"""
        candidates = [self._chunk("1_0", 1)]
        self.milvus.search.return_value = candidates
        self.documents.get_documents_by_ids.return_value = [
            self._doc(
                1,
                DocumentStatus.SUCCESS,
                source_type="upload",
            ),
        ]

        results = self._search()

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertIsNone(result.title)
        self.assertIsNone(result.url)
        self.assertEqual(result.source_type, "upload")

    def test_o_metadata_scrambled_order_webclip(self) -> None:
        """O：顺序打乱时 title/url/source_type 仍按 document_map[id] 正确匹配。"""
        candidates = [self._chunk("1_0", 1), self._chunk("2_0", 2)]
        self.milvus.search.return_value = candidates
        # 返回顺序与 page_ids=[1,2] 相反
        self.documents.get_documents_by_ids.return_value = [
            self._doc(
                2,
                DocumentStatus.SUCCESS,
                title="Doc-2",
                url="https://example.com/2",
                source_type="webpage",
            ),
            self._doc(
                1,
                DocumentStatus.SUCCESS,
                title="Doc-1",
                url="https://example.com/1",
                source_type="webpage",
            ),
        ]

        results = self._search()

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].title, "Doc-1")
        self.assertEqual(results[0].url, "https://example.com/1")
        self.assertEqual(results[1].title, "Doc-2")
        self.assertEqual(results[1].url, "https://example.com/2")
        self.assertTrue(all(r.source_type == "webpage" for r in results))

    def test_p_orphan_filter_unchanged_with_metadata(self) -> None:
        """P：metadata 不改变 orphan filter（SUCCESS-only filter 保持原行为）。"""
        candidates = [self._chunk("1_0", 1), self._chunk("999_0", 999)]
        self.milvus.search.return_value = candidates
        self.documents.get_success_document_ids.return_value = [1, 999]
        self.documents.get_documents_by_ids.return_value = [
            self._doc(
                1,
                DocumentStatus.SUCCESS,
                title="Doc-1",
                url="https://example.com/1",
                source_type="webpage",
            ),
        ]

        results = self._search()

        self.assertEqual([r.id for r in results], ["1_0"])
        self.documents.get_documents_by_ids.assert_called_once_with([1, 999], "plugin-a")


class RagServiceUserIsolationTest(unittest.TestCase):
    """
    Phase 3.5 Step 2-E：RAG 检索按插件工作空间隔离（全部知识库模式 + 当前网页模式）。

    覆盖隔离用例 1-10（user 隔离迁移为 plugin 隔离）：
      1. plugin-a 全库只检索 plugin-a（success_ids=[1]；混入 page_id=999 → 剔除）
      2. plugin-b 全库只检索 plugin-b（success_ids=[2]；混入 page_id=1 → 剔除）
      3. success_ids 正确构造 Milvus expr（expr="page_id in [1,2]"）
      4. 混入其他 Workspace page_id → 应用层 post-filter 删除（双保险）
      5. 当前网页模式 expr == "page_id == {document_id}"
      6. 当前网页模式 ownership 失败（跨 Workspace）→ DocumentNotFoundError
      7. 非 SUCCESS → DocumentNotSuccessError
      8. success_ids=[] → 不调用 Milvus，直接返回 []
      9. limit 仍正确
      10. document_id 模式 candidate_limit 仍为 max(limit*4, 40)
    """

    def setUp(self) -> None:
        self.embedding = Mock(spec=EmbeddingClient)
        self.embedding.embed.return_value = [_QUERY_VECTOR]
        self.milvus = Mock(spec=MilvusRepository)
        self.documents = Mock(spec=DocumentRepository)
        self.service = RagService(
            embedding_client=self.embedding,
            milvus_repository=self.milvus,
            document_repository=self.documents,
        )

    def _chunk(self, pk: str, page_id: int, index: int = 0) -> ChunkSearchResult:
        return ChunkSearchResult(
            id=pk,
            page_id=page_id,
            chunk_index=index,
            chunk_text=f"text-{pk}",
            distance=0.1,
        )

    def _doc(self, doc_id: int, status: str) -> Document:
        return Document(
            id=doc_id,
            filename=f"file-{doc_id}.txt",
            file_path=f"uploads/file-{doc_id}.txt",
            status=status,
        )

    def _search(
        self,
        query: str = "hello",
        limit: int = 5,
        document_id: int | None = None,
        plugin_id: str | None = "plugin-a",
    ) -> list[RagSearchResult]:
        return asyncio.run(
            self.service.search(
                query=query,
                limit=limit,
                document_id=document_id,
                plugin_id=plugin_id,
            )
        )

    # 1. plugin-a 全库只检索 plugin-a（Milvus 混入其他 Workspace page_id=999，双保险剔除）
    def test_plugin_a_full_knowledge_only_sees_a(self) -> None:
        self.documents.get_success_document_ids.return_value = [1]
        self.milvus.search.return_value = [
            self._chunk("1_0", 1),
            self._chunk("999_0", 999),
        ]
        self.documents.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS),
        ]

        results = self._search()

        # 应用层兜底：999 不属于 plugin-a 的 success_ids → 被剔除
        self.assertEqual([r.id for r in results], ["1_0"])
        self.documents.get_success_document_ids.assert_called_once_with("plugin-a")
        self.milvus.search.assert_called_once_with(
            _QUERY_VECTOR, limit=10, expr="page_id in [1]"
        )

    # 2. plugin-b 全库只检索 plugin-b
    def test_plugin_b_full_knowledge_only_sees_b(self) -> None:
        self.documents.get_success_document_ids.return_value = [2]
        self.milvus.search.return_value = [
            self._chunk("1_0", 1),
            self._chunk("2_0", 2),
        ]
        self.documents.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS),
            self._doc(2, DocumentStatus.SUCCESS),
        ]

        results = self._search(plugin_id="plugin-b")

        # 即使 SQL 层（mock）返回了两份 SUCCESS 文档，post-filter 仍以
        # plugin-b 的 success_ids=[2] 为唯一允许集合 → 剔除 page 1。
        self.assertEqual([r.id for r in results], ["2_0"])
        self.documents.get_success_document_ids.assert_called_once_with("plugin-b")
        self.milvus.search.assert_called_once_with(
            _QUERY_VECTOR, limit=10, expr="page_id in [2]"
        )

    # 3. success_ids 正确构造 Milvus expr
    def test_success_ids_build_milvus_expr(self) -> None:
        self.documents.get_success_document_ids.return_value = [1, 2]
        self.milvus.search.return_value = [
            self._chunk("1_0", 1),
            self._chunk("2_0", 2),
        ]
        self.documents.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS),
            self._doc(2, DocumentStatus.SUCCESS),
        ]

        self._search()

        self.milvus.search.assert_called_once_with(
            _QUERY_VECTOR, limit=10, expr="page_id in [1,2]"
        )

    # 4. 混入其他 Workspace page_id → post-filter 删除（即使下游全放行也兜底）
    def test_post_filter_removes_other_workspace_page(self) -> None:
        self.documents.get_success_document_ids.return_value = [1, 2]
        # Milvus 异常返回三个候选（含其他 Workspace page 999）
        self.milvus.search.return_value = [
            self._chunk("1_0", 1),
            self._chunk("2_0", 2),
            self._chunk("999_0", 999),
        ]
        # 极端场景：mock SQL 层也“错误”放行 999（真实不会发生），
        # 应用层 page_id in success_ids 兜底必须删除 999。
        self.documents.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS),
            self._doc(2, DocumentStatus.SUCCESS),
            self._doc(999, DocumentStatus.SUCCESS),
        ]

        results = self._search()

        self.assertEqual({r.id for r in results}, {"1_0", "2_0"})
        self.assertEqual({r.page_id for r in results}, {1, 2})

    # 5. 当前网页模式 expr == page_id == document_id
    def test_current_page_mode_expr(self) -> None:
        self.documents.get_document.return_value = self._doc(
            1, DocumentStatus.SUCCESS
        )
        self.milvus.search.return_value = [self._chunk("1_0", 1)]
        self.documents.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS),
        ]

        results = self._search(document_id=1, limit=5)

        self.assertEqual([r.id for r in results], ["1_0"])
        self.documents.get_document.assert_called_once_with(1, "plugin-a")
        # candidate_limit = max(5*4, 40) = 40
        self.milvus.search.assert_called_once_with(
            _QUERY_VECTOR, limit=40, expr="page_id == 1"
        )

    # 6. 当前网页模式 ownership 失败（跨 Workspace / 不存在）→ DocumentNotFoundError
    def test_current_page_mode_other_workspace_document_raises(self) -> None:
        # get_document(document_id, plugin_id) 带归属过滤：他人文档返回 None
        self.documents.get_document.return_value = None

        with self.assertRaises(DocumentNotFoundError):
            self._search(document_id=999, plugin_id="plugin-a")

        self.milvus.search.assert_not_called()

    # 7. 非 SUCCESS → DocumentNotSuccessError
    def test_current_page_mode_non_success_raises(self) -> None:
        self.documents.get_document.return_value = self._doc(
            1, DocumentStatus.FAILED
        )

        with self.assertRaises(DocumentNotSuccessError):
            self._search(document_id=1, plugin_id="plugin-a")

        self.milvus.search.assert_not_called()

    # 8. success_ids=[] → 不调用 Milvus
    def test_empty_success_ids_skips_milvus(self) -> None:
        self.documents.get_success_document_ids.return_value = []

        results = self._search()

        self.assertEqual(results, [])
        self.milvus.search.assert_not_called()
        self.documents.get_documents_by_ids.assert_not_called()

    # 9. limit 仍正确（全库模式 top-K）
    def test_full_knowledge_limit_respected(self) -> None:
        self.documents.get_success_document_ids.return_value = [1, 2]
        self.milvus.search.return_value = [
            self._chunk(f"{i}_0", 1 if i % 2 else 2) for i in range(5)
        ]
        self.documents.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS),
            self._doc(2, DocumentStatus.SUCCESS),
        ]

        results = self._search(limit=2)

        self.assertEqual(len(results), 2)
        self.milvus.search.assert_called_once_with(
            _QUERY_VECTOR, limit=10, expr="page_id in [1,2]"
        )

    # 10. document_id 模式 candidate_limit = max(limit*4, 40)
    def test_current_page_candidate_limit(self) -> None:
        self.documents.get_document.return_value = self._doc(
            1, DocumentStatus.SUCCESS
        )
        self.milvus.search.return_value = [self._chunk("1_0", 1)]
        self.documents.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS),
        ]

        results = self._search(document_id=1, limit=1)

        self.assertEqual(len(results), 1)
        # max(1*4, 40) = 40
        self.milvus.search.assert_called_once_with(
            _QUERY_VECTOR, limit=40, expr="page_id == 1"
        )

    # 全库模式匿名（plugin_id=None）→ 拒绝并返回 []（不调用 Milvus / MySQL）
    def test_anonymous_full_knowledge_rejected(self) -> None:
        results = asyncio.run(
            self.service.search(query="hello", limit=5, plugin_id=None)
        )

        self.assertEqual(results, [])
        self.documents.get_success_document_ids.assert_not_called()
        self.milvus.search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
