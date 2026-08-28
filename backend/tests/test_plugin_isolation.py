"""
插件工作空间（plugin_id）维度隔离测试（Phase 3.5 Step 2-E）。

背景：
    Phase 3.5 将全链路的归属标识从 user_id 迁移为 plugin_id。本测试从
    「插件维度隔离」的视角验证：不同 Plugin Workspace 之间不得相互访问
    / 检索 / 删除彼此的文档，API Key 解密也仅针对当前插件工作空间。

覆盖：
    A. Repository 层（真实 SQLite in-memory + DocumentRepositoryImpl）：
        1.  A 创建的文档，B 通过 get_document 读取 → DocumentNotFoundError；
        2.  get_success_document_ids 只返回本插件 SUCCESS 文档（不含他插件）；
        3.  get_documents_by_ids 混入他插件 id → SQL 层过滤，只返回本插件；
        4.  A 删除 B 的文档 → DocumentNotFoundError（B 文档仍在）。
    B. Service 层（Mock 依赖注入，验证 plugin_id 传递与跨插件拒绝）：
        5.  RagService.search 全库模式：Milvus 混入他插件候选 → 双保险剔除，
            get_success_document_ids / get_documents_by_ids 均以本插件 plugin_id 调用；
        6.  RagService.search 当前网页模式：A 以 B 的 document_id 检索 →
            DocumentNotFoundError（ownership check 在 Milvus 之前）；
        7.  RagAnswerService.ask：document_id 属于 B、以 A 身份 → DocumentNotFoundError，
            LLM 不被调用；
        8.  WebClipService.clip：create_document 以 plugin_id 落库；
        9.  DocumentUploadService.upload：create_document 以 plugin_id 落库；
        10. DocumentIngestService：A ingest B 的文档 → DocumentNotFoundError，
            不进入 Milvus；
        11. DocumentDeleteService：A 删除 B 的文档 → 幂等成功（按「不存在」
            处理，不泄露归属，Milvus / FileStorage 均不被调用）。

技术栈：unittest + unittest.mock；不连接真实 MySQL / Milvus / 百炼。
Repository 层使用 SQLite in-memory + StaticPool（与 test_document_repository.py 相同）。
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import StaticPool

from backend.chunkers import Chunker
from backend.clients.embedding import EmbeddingClient
from backend.core.exceptions import DocumentNotFoundError
from backend.models.base import Base
from backend.models.document import Document, DocumentStatus
from backend.models.milvus_dto import ChunkSearchResult
from backend.parsers import DocumentParser
from backend.repositories.milvus import MilvusRepository
from backend.repositories.mysql import DocumentRepository, DocumentRepositoryImpl
from backend.services.document_delete import DocumentDeleteService
from backend.services.document_ingest import DocumentIngestService
from backend.services.document_upload import DocumentUploadService
from backend.services.ingest import IngestService
from backend.services.rag import RagService
from backend.services.rag_answer import RagAnswerService
from backend.services.web_clip import WebClipService
from backend.storage import FileStorage

PLUGIN_A = "plugin-a-4f0d1c2b3a99887766554433221100ffeeddccbbaa"
PLUGIN_B = "plugin-b-00aa11bb22cc33dd44ee55ff66778899aabbccdd"

_QUERY_VECTOR: list[float] = [0.1] * 1024


def _make_test_engine() -> Engine:
    """构造 SQLite in-memory engine（StaticPool 保证多连接共享同一内存库）。"""
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


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


# ======================================================================
# A. Repository 层插件隔离（真实 SQLite）
# ======================================================================
class PluginIsolationRepositoryTest(unittest.TestCase):
    """DocumentRepositoryImpl 的 plugin_id 维度隔离测试。"""

    def setUp(self) -> None:
        self.engine: Engine = _make_test_engine()
        Base.metadata.create_all(self.engine)
        _seed_plugin_workspaces(self.engine)
        self.repo: DocumentRepositoryImpl = DocumentRepositoryImpl(self.engine)

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _create_success_doc(self, plugin_id: str, filename: str) -> Document:
        doc = self.repo.create_document(
            filename=filename,
            file_path=f"/data/{filename}",
            plugin_id=plugin_id,
        )
        self.repo.update_status(doc.id, DocumentStatus.SUCCESS)
        return self.repo.get_document(doc.id, plugin_id)

    # ------------------------------------------------------- 1. get 跨插件
    def test_get_document_cross_plugin_not_found(self) -> None:
        """A(1)：A 创建的文档，B 读取 → DocumentNotFoundError。"""
        doc_a = self.repo.create_document(
            filename="a.txt", file_path="/data/a.txt", plugin_id=PLUGIN_A
        )

        # A 可读
        self.assertEqual(
            self.repo.get_document(doc_a.id, PLUGIN_A).id, doc_a.id
        )
        # B 读 A 的文档 → 404 语义（不存在）
        with self.assertRaises(DocumentNotFoundError):
            self.repo.get_document(doc_a.id, PLUGIN_B)

    # ----------------------------------------- 2. get_success_document_ids
    def test_get_success_document_ids_scoped_by_plugin(self) -> None:
        """A(2)：get_success_document_ids 只返回本插件 SUCCESS 文档。"""
        self._create_success_doc(PLUGIN_A, "a1.txt")
        self._create_success_doc(PLUGIN_A, "a2.txt")
        self._create_success_doc(PLUGIN_B, "b1.txt")
        # A 还有一个非 SUCCESS 文档
        pending = self.repo.create_document(
            filename="a-pending.txt",
            file_path="/data/a-pending.txt",
            plugin_id=PLUGIN_A,
        )
        self.assertNotEqual(pending.status, DocumentStatus.SUCCESS)

        success_a = self.repo.get_success_document_ids(PLUGIN_A)
        success_b = self.repo.get_success_document_ids(PLUGIN_B)

        # A 只包含 A 的 SUCCESS 文档（2 个），不包含 B 的与 A 的 PENDING
        self.assertEqual(len(success_a), 2)
        self.assertNotIn(pending.id, success_a)
        # B 只包含 B 的 SUCCESS 文档（1 个）
        self.assertEqual(len(success_b), 1)

    # ------------------------------------------- 3. get_documents_by_ids
    def test_get_documents_by_ids_scoped_by_plugin(self) -> None:
        """A(3)：get_documents_by_ids 混入他插件 id → SQL 层过滤，只返回本插件。"""
        doc_a = self._create_success_doc(PLUGIN_A, "a.txt")
        doc_b = self._create_success_doc(PLUGIN_B, "b.txt")

        docs = self.repo.get_documents_by_ids(
            [doc_a.id, doc_b.id], PLUGIN_A
        )

        self.assertEqual([d.id for d in docs], [doc_a.id])

    # --------------------------------------------- 4. delete 跨插件
    def test_delete_document_cross_plugin_not_found(self) -> None:
        """A(4)：A 删除 B 的文档 → DocumentNotFoundError，B 文档仍在。"""
        doc_b = self._create_success_doc(PLUGIN_B, "b.txt")

        with self.assertRaises(DocumentNotFoundError):
            self.repo.delete_document(doc_b.id, PLUGIN_A)

        # B 的文档仍在（B 可读）
        self.assertEqual(
            self.repo.get_document(doc_b.id, PLUGIN_B).id, doc_b.id
        )


# ======================================================================
# B. Service 层插件隔离（Mock 依赖）
# ======================================================================
class PluginIsolationServiceTest(unittest.TestCase):
    """业务 Service 的 plugin_id 传递与跨插件拒绝测试。"""

    def setUp(self) -> None:
        """构造统一 Mock 依赖集合（各测试按需复用）。"""
        self.document_repo = Mock(spec=DocumentRepository)
        self.milvus = Mock(spec=MilvusRepository)
        self.embedding = Mock(spec=EmbeddingClient)
        self.embedding.embed.return_value = [_QUERY_VECTOR]
        self.ingest_service = Mock(spec=IngestService)
        self.file_storage = Mock(spec=FileStorage)

    def run_async(self, coro) -> object:
        return asyncio.run(coro)

    # ------------------------------------------- 5. RagService 全库模式
    def test_rag_search_full_knowledge_scoped_by_plugin(self) -> None:
        """B(5)：全库模式只检索本插件；Milvus 混入他插件候选 → 双保险剔除。"""
        self.document_repo.get_success_document_ids.return_value = [1, 2]
        # Milvus 异常返回三个候选（含 B 的 page_id=99）
        self.milvus.search.return_value = [
            self._chunk("1_0", 1),
            self._chunk("2_0", 2),
            self._chunk("99_0", 99),
        ]
        self.document_repo.get_documents_by_ids.return_value = [
            self._doc(1, DocumentStatus.SUCCESS),
            self._doc(2, DocumentStatus.SUCCESS),
        ]

        service = RagService(
            embedding_client=self.embedding,
            milvus_repository=self.milvus,
            document_repository=self.document_repo,
        )
        results = self.run_async(
            service.search(query="hello", limit=5, plugin_id=PLUGIN_A)
        )

        self.assertEqual([c.id for c in results], ["1_0", "2_0"])
        # 两个 Repository 调用均以本插件 plugin_id 执行（SQL 层隔离）
        self.document_repo.get_success_document_ids.assert_called_once_with(
            PLUGIN_A
        )
        self.document_repo.get_documents_by_ids.assert_called_once_with(
            [1, 2, 99], PLUGIN_A
        )

    # ------------------------------------------- 6. RagService 网页模式
    def test_rag_search_current_page_cross_plugin_404(self) -> None:
        """B(6)：A 以 B 的 document_id 检索 → DocumentNotFoundError（在 Milvus 之前）。"""
        self.document_repo.get_document.side_effect = DocumentNotFoundError(
            "Document 99 不存在或不属于当前插件工作空间"
        )

        service = RagService(
            embedding_client=self.embedding,
            milvus_repository=self.milvus,
            document_repository=self.document_repo,
        )
        with self.assertRaises(DocumentNotFoundError):
            self.run_async(
                service.search(
                    query="hello", limit=5, document_id=99, plugin_id=PLUGIN_A
                )
            )

        # ownership check 以 (document_id, plugin_id) 执行；未触发 Milvus
        self.document_repo.get_document.assert_called_once_with(99, PLUGIN_A)
        self.milvus.search.assert_not_called()

    # ------------------------------------------- 7. RagAnswerService.ask
    def test_rag_answer_cross_plugin_document_404(self) -> None:
        """B(7)：ask 指定属于 B 的 document_id、以 A 身份 → 404，LLM 不被调用。"""
        self.document_repo.get_document.side_effect = DocumentNotFoundError(
            "Document 99 不存在或不属于当前插件工作空间"
        )
        rag_service = Mock(spec=RagService)
        llm_client = Mock()
        service = RagAnswerService(
            rag_service=rag_service,
            llm_client=llm_client,
            document_repository=self.document_repo,
        )

        with self.assertRaises(DocumentNotFoundError):
            self.run_async(
                service.ask(query="问题", document_id=99, plugin_id=PLUGIN_A)
            )

        self.document_repo.get_document.assert_called_once_with(99, PLUGIN_A)
        rag_service.search.assert_not_awaited()
        llm_client.generate.assert_not_called()

    # ------------------------------------------- 8. WebClipService.clip
    def test_web_clip_creates_document_with_plugin_id(self) -> None:
        """B(8)：clip 以 plugin_id 落库 create_document + ingest_document。"""
        self.document_repo.create_document.return_value = self._doc(
            1, DocumentStatus.PENDING
        )
        self.document_repo.get_document.return_value = self._doc(
            1, DocumentStatus.SUCCESS
        )
        chunker = Mock(spec=Chunker)
        chunker.split.return_value = ["chunk-1"]
        document_ingest = Mock(spec=DocumentIngestService)
        document_ingest.ingest_document = AsyncMock(return_value=None)

        service = WebClipService(
            document_repository=self.document_repo,
            chunker=chunker,
            document_ingest_service=document_ingest,
        )
        self.run_async(
            service.clip(
                url="https://example.com/page",
                raw_text="正文",
                plugin_id=PLUGIN_B,
                title="标题",
            )
        )

        self.document_repo.create_document.assert_called_once()
        _, kwargs = self.document_repo.create_document.call_args
        self.assertEqual(kwargs["plugin_id"], PLUGIN_B)
        document_ingest.ingest_document.assert_awaited_once_with(
            1, ["chunk-1"], plugin_id=PLUGIN_B, api_key=None
        )

    # ------------------------------------------- 9. DocumentUploadService
    def test_upload_creates_document_with_plugin_id(self) -> None:
        """B(9)：upload 以 plugin_id 落库 create_document + ingest_document。"""
        self.document_repo.create_document.return_value = self._doc(
            1, DocumentStatus.PENDING
        )
        self.document_repo.get_document.return_value = self._doc(
            1, DocumentStatus.SUCCESS
        )
        self.file_storage.save.return_value = "abc.txt"
        self.file_storage.resolve.return_value = "uploads/txt/abc.txt"
        parser = Mock(spec=DocumentParser)
        parser.parse.return_value = "hello world"
        chunker = Mock(spec=Chunker)
        chunker.split.return_value = ["hello world"]
        document_ingest = Mock(spec=DocumentIngestService)
        document_ingest.ingest_document = AsyncMock(return_value=None)

        service = DocumentUploadService(
            document_repository=self.document_repo,
            file_storage=self.file_storage,
            parser=parser,
            chunker=chunker,
            document_ingest_service=document_ingest,
            max_content_bytes=100,
        )
        self.run_async(
            service.upload(
                filename="test.txt",
                content=b"hello world",
                plugin_id=PLUGIN_A,
                mime_type="text/plain",
            )
        )

        self.document_repo.create_document.assert_called_once()
        _, kwargs = self.document_repo.create_document.call_args
        self.assertEqual(kwargs["plugin_id"], PLUGIN_A)
        document_ingest.ingest_document.assert_awaited_once_with(
            1, ["hello world"], plugin_id=PLUGIN_A, api_key=None
        )

    # ------------------------------------------- 10. DocumentIngestService
    def test_ingest_document_cross_plugin_not_found(self) -> None:
        """B(10)：A ingest B 的文档 → DocumentNotFoundError，不进入 Milvus。"""
        self.document_repo.get_document.side_effect = DocumentNotFoundError(
            "document not found: id=8"
        )
        service = DocumentIngestService(
            document_repository=self.document_repo,
            ingest_service=self.ingest_service,
        )

        with self.assertRaises(DocumentNotFoundError):
            self.run_async(
                service.ingest_document(
                    document_id=8,
                    chunks=["c1"],
                    plugin_id=PLUGIN_A,
                    api_key="sk",
                )
            )

        self.document_repo.get_document.assert_called_once_with(8, PLUGIN_A)
        self.ingest_service.ingest_page.assert_not_called()

    # ------------------------------------------- 11. DocumentDeleteService
    def test_delete_document_cross_plugin_idempotent(self) -> None:
        """B(11)：A 删除 B 的文档 → 幂等成功（按「不存在」处理），不调 Milvus/FileStorage。"""
        self.document_repo.get_document.side_effect = DocumentNotFoundError(
            "document not found: id=8"
        )
        service = DocumentDeleteService(
            document_repository=self.document_repo,
            milvus_repository=self.milvus,
            file_storage=self.file_storage,
        )

        # 幂等成功：不抛异常
        self.run_async(service.delete_document(8, PLUGIN_A))

        self.document_repo.get_document.assert_called_once_with(8, PLUGIN_A)
        self.milvus.delete_chunks.assert_not_called()
        self.file_storage.delete.assert_not_called()
        self.document_repo.delete_document.assert_not_called()

    # ------------------------------------------------------------------ helpers
    def _chunk(self, pk: str, page_id: int) -> ChunkSearchResult:
        return ChunkSearchResult(
            id=pk,
            page_id=page_id,
            chunk_index=0,
            chunk_text=f"text-{pk}",
            distance=0.1,
        )

    def _doc(self, doc_id: int, status: str) -> Document:
        return Document(
            id=doc_id,
            plugin_id=PLUGIN_A,
            filename=f"file-{doc_id}.txt",
            file_path=f"uploads/file-{doc_id}.txt",
            status=status,
        )


if __name__ == "__main__":
    unittest.main()
