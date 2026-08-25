"""
DocumentRepository 的 SQLAlchemy 实现（Phase 2.9 Step 1；Phase 3.4 Step C user-aware）。

Phase 3.4 Step C 变更：
- create_document：user_id 必填（不允许默认 None），Repository 不推断；
- get_document(document_id, user_id)：WHERE id + user_id 双条件，跨用户按
  「不存在」处理（DocumentNotFoundError，不泄露归属）；
- get_documents_by_ids(document_ids, user_id)：SQL 层 IN + user_id 过滤；
- 新增 get_success_document_ids(user_id)：只返回 SUCCESS 的 id 列表；
- delete_document(document_id, user_id)：WHERE id + user_id，跨用户按
  「不存在」处理；返回被删除的 Document（detached）。
- update_status / update_ingest_result / update_failure 签名不变：调用方必须
  先经 get_document(document_id, user_id) ownership check 成功后再操作。

设计要点：
1) 注入 Engine（而非 Settings），便于单元测试用 SQLite 替换 engine；
2) 内部 sessionmaker 用 expire_on_commit=False，保证 commit 后返回的
   detached ORM 对象属性仍可读；
3) Session 生命周期：每个方法内部 `with self._session_factory() as session:`
   管理，方法结束自动 close；
4) 不吞异常：所有 SQLAlchemy 原生异常包装为 DocumentRepositoryError 族
   后抛出，保留 `raise ... from e` 异常链；
5) update_status 前置校验 status ∈ DocumentStatus.ALL，非法值不发 SQL。

TODO（后续 Phase）：当前 Document 无 relationship 字段，commit 后返回
detached ORM 对象安全。后续增加 Document 与 Page/Chunk relationship 后，
需要考虑 DTO 转换或 eager loading，避免 DetachedInstanceError。
"""

from __future__ import annotations

from sqlalchemy import Engine, select
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from ...core.exceptions import (
    DocumentNotFoundError,
    DocumentOperationError,
)
from ...models.document import Document, DocumentStatus
from .protocol import DocumentRepository, _UNSET


class DocumentRepositoryImpl(DocumentRepository):
    """DocumentRepository 的 SQLAlchemy 实现。"""

    def __init__(self, engine: Engine) -> None:
        """
        注入 Engine，内部构造 sessionmaker。

        Args:
            engine: SQLAlchemy Engine（生产环境来自 core.db.get_engine；
                    测试环境可传 SQLite engine，如 StaticPool in-memory）。
        """
        self._engine = engine
        # expire_on_commit=False：commit 后不自动 expire，detached ORM 属性仍可读。
        # TODO(后续 Phase): 当前 Document 无 relationship，detached 安全。
        #   未来增加 Document↔Page/Chunk relationship 后，需 DTO 转换或 eager
        #   loading，避免 DetachedInstanceError。
        self._session_factory: sessionmaker[Session] = sessionmaker(
            bind=engine,
            expire_on_commit=False,
        )

    # ------------------------------------------------------------------ create
    def create_document(
        self,
        filename: str,
        file_path: str,
        user_id: int,
        file_size: int | None = None,
        mime_type: str | None = None,
        title: str | None = None,
        url: str | None = None,
        source_type: str | None = None,
    ) -> Document:
        """
        插入一条 Document 记录，返回 detached ORM 对象。

        Phase 3.4 Step C：user_id **必填**（不允许默认 None）。Repository
        不推断 / 不生成 user_id，必须由上层认证上下文提供；只负责写入。

        file_size / mime_type 为 Phase 2.10 Step 3 上传路径可选参数：
        显式传入时写入对应列；为 None 时不赋值，保持 ORM default
        （file_size=0 / mime_type=""），不影响既有 POST /documents 调用。

        title / url / source_type 为 Phase 3.1 Step 3 网页剪藏路径可选参数：
        - title / url：显式传入时写入；None 时保持 ORM 默认（NULL）；
        - source_type：显式传入（webpage）时写入；None 时保持 ORM default
          "upload"，既有上传链路调用完全兼容。
        """
        document = Document(
            filename=filename,
            file_path=file_path,
            user_id=user_id,
        )
        if file_size is not None:
            document.file_size = file_size
        if mime_type is not None:
            document.mime_type = mime_type
        if title is not None:
            document.title = title
        if url is not None:
            document.url = url
        if source_type is not None:
            document.source_type = source_type
        try:
            with self._session_factory() as session:
                session.add(document)
                session.commit()
                session.refresh(document)
                return document
        except (OperationalError, IntegrityError, DBAPIError) as e:
            raise DocumentOperationError(
                f"create_document failed: filename={filename!r}, "
                f"user_id={user_id!r}, error={e!s}"
            ) from e

    # -------------------------------------------------------------------- get
    def get_document(self, document_id: int, user_id: int) -> Document:
        """
        按主键 + 归属用户查询 Document（user-aware）。

        SQL：WHERE id = :document_id AND user_id = :user_id。
        用户 A 查询用户 B 的 document_id → 查不到 → DocumentNotFoundError
        （等同「不存在」，不返回 B 的文档、不泄露归属）。
        """
        try:
            with self._session_factory() as session:
                document = (
                    session.execute(
                        select(Document).where(
                            Document.id == document_id,
                            Document.user_id == user_id,
                        )
                    )
                    .scalars()
                    .first()
                )
                if document is None:
                    raise DocumentNotFoundError(
                        f"document not found: id={document_id}"
                    )
                return document
        except DocumentNotFoundError:
            raise
        except (OperationalError, DBAPIError) as e:
            raise DocumentOperationError(
                f"get_document failed: id={document_id}, "
                f"user_id={user_id!r}, error={e!s}"
            ) from e

    # ---------------------------------------------------------------- get_many
    def get_documents_by_ids(
        self,
        document_ids: list[int],
        user_id: int,
    ) -> list[Document]:
        """
        按主键集合 + 归属用户批量查询 Document（user-aware）。

        行为约定：
        - SQL 层直接过滤：WHERE id IN (...) AND user_id = :user_id，
          不允许先查全部再 Python 过滤（不 N+1）；
        - document_ids 为空时直接返回 []，不发 SQL；
        - 使用一次批量 IN 查询，不逐个调用 get_document()；
        - 输入中不存在的 ID 直接忽略、不抛 DocumentNotFoundError
          （RagService 依赖此语义过滤孤儿 chunk）；
        - 返回 detached ORM 对象，属性可安全读取。
        """
        if not document_ids:
            return []
        try:
            with self._session_factory() as session:
                documents = (
                    session.execute(
                        select(Document).where(
                            Document.id.in_(document_ids),
                            Document.user_id == user_id,
                        )
                    )
                    .scalars()
                    .all()
                )
                return list(documents)
        except (OperationalError, DBAPIError) as e:
            raise DocumentOperationError(
                f"get_documents_by_ids failed: "
                f"ids={document_ids!r}, user_id={user_id!r}, error={e!s}"
            ) from e

    # ------------------------------------------------------- get_success_ids
    def get_success_document_ids(self, user_id: int) -> list[int]:
        """
        返回指定用户 status == SUCCESS 的 Document id 列表（user-aware）。

        SQL：SELECT id FROM documents WHERE user_id = :user_id
             AND status = :status_success。
        只返回 id（不构造完整 Document，省内存，单次 SQL）；
        没结果返回 []（不抛异常）。
        """
        try:
            with self._session_factory() as session:
                ids = (
                    session.execute(
                        select(Document.id).where(
                            Document.user_id == user_id,
                            Document.status == DocumentStatus.SUCCESS,
                        )
                    )
                    .scalars()
                    .all()
                )
                return list(ids)
        except (OperationalError, DBAPIError) as e:
            raise DocumentOperationError(
                f"get_success_document_ids failed: "
                f"user_id={user_id!r}, error={e!s}"
            ) from e

    # ----------------------------------------------------------- update_status
    def update_status(
        self,
        doc_id: int,
        status: str,
        *,
        error_message: object = _UNSET,
    ) -> Document:
        """
        更新 Document.status（可选同时更新 error_message，sentinel 语义）。

        - 不传 error_message：保持原 error_message 不变（向后兼容）；
        - error_message=None：清空 error_message；
        - error_message="xxx"：写入 error_message。
        不修改 chunk_count。前置校验 status 合法性，不存在抛
        DocumentNotFoundError。
        """
        if status not in DocumentStatus.ALL:
            raise DocumentOperationError(
                f"invalid status: {status!r}, "
                f"allowed: {sorted(DocumentStatus.ALL)}"
            )
        try:
            with self._session_factory() as session:
                document = session.get(Document, doc_id)
                if document is None:
                    raise DocumentNotFoundError(
                        f"document not found: id={doc_id}"
                    )
                document.status = status
                if error_message is not _UNSET:
                    document.error_message = error_message
                session.commit()
                session.refresh(document)
                return document
        except DocumentNotFoundError:
            raise
        except (OperationalError, IntegrityError, DBAPIError) as e:
            raise DocumentOperationError(
                f"update_status failed: id={doc_id}, status={status!r}, "
                f"error={e!s}"
            ) from e

    # ------------------------------------------------------ update_ingest_result
    def update_ingest_result(
        self,
        document_id: int,
        *,
        chunk_count: int,
        status: str,
        error_message: object = _UNSET,
    ) -> Document:
        """
        在**同一个数据库事务**中一次更新 Document.chunk_count 与 status
        （可选同时更新 error_message，sentinel 语义）。

        Phase 2.9 Step 2：DocumentIngestService 成功路径原子提交，
        统一完成 `chunk_count + status=SUCCESS`，禁止两步拆分。

        Phase 2.11 Step 2 扩展 error_message：
        - 不传 error_message：保持原 error_message 不变（向后兼容）；
        - error_message=None：清空 error_message（成功路径清空旧错误）；
        - error_message="xxx"：写入 error_message。

        实现要点：
        - 单个 Session 内依次设置 chunk_count、status、error_message 后一次
          session.commit()，保证字段变更属于同一事务（要么同时生效，要么同时回滚）；
        - 前置校验 status ∈ DocumentStatus.ALL，非法值不发 SQL；
        - 不存在抛 DocumentNotFoundError；SQLAlchemy 异常包装后保留异常链。
        """
        if status not in DocumentStatus.ALL:
            raise DocumentOperationError(
                f"invalid status: {status!r}, "
                f"allowed: {sorted(DocumentStatus.ALL)}"
            )
        try:
            with self._session_factory() as session:
                document = session.get(Document, document_id)
                if document is None:
                    raise DocumentNotFoundError(
                        f"document not found: id={document_id}"
                    )
                document.chunk_count = chunk_count
                document.status = status
                if error_message is not _UNSET:
                    document.error_message = error_message
                session.commit()
                session.refresh(document)
                return document
        except DocumentNotFoundError:
            raise
        except (OperationalError, IntegrityError, DBAPIError) as e:
            raise DocumentOperationError(
                f"update_ingest_result failed: id={document_id}, "
                f"chunk_count={chunk_count!r}, status={status!r}, "
                f"error={e!s}"
            ) from e

    # ------------------------------------------------------------ update_failure
    def update_failure(
        self,
        document_id: int,
        *,
        error_message: str,
    ) -> Document:
        """
        在**同一个数据库事务**中一次更新 Document.status=FAILED 与 error_message
        （Phase 2.10 Step 3，UploadService 失败路径原子落盘）。

        实现要点：
        - 单个 Session 内依次设置 status、error_message 后一次 commit()，
          保证两条字段变更属于同一事务；
        - **不修改 chunk_count**（保持创建时的 0），避免失败路径误写；
        - 不存在抛 DocumentNotFoundError；SQLAlchemy 异常包装后保留异常链。
        """
        try:
            with self._session_factory() as session:
                document = session.get(Document, document_id)
                if document is None:
                    raise DocumentNotFoundError(
                        f"document not found: id={document_id}"
                    )
                document.status = DocumentStatus.FAILED
                document.error_message = error_message
                session.commit()
                session.refresh(document)
                return document
        except DocumentNotFoundError:
            raise
        except (OperationalError, IntegrityError, DBAPIError) as e:
            raise DocumentOperationError(
                f"update_failure failed: id={document_id}, "
                f"error_message_len={len(error_message)}, "
                f"error={e!s}"
            ) from e

    # ------------------------------------------------------------------ delete
    def delete_document(self, document_id: int, user_id: int) -> Document:
        """
        按主键 + 归属用户删除 Document（user-aware）；返回被删对象（detached）。

        SQL：WHERE id = :document_id AND user_id = :user_id。
        用户 A 删除用户 B 的 document → 查不到 → DocumentNotFoundError
        （按「不存在」处理，不删除、不泄露归属）。
        """
        try:
            with self._session_factory() as session:
                document = (
                    session.execute(
                        select(Document).where(
                            Document.id == document_id,
                            Document.user_id == user_id,
                        )
                    )
                    .scalars()
                    .first()
                )
                if document is None:
                    raise DocumentNotFoundError(
                        f"document not found: id={document_id}"
                    )
                session.delete(document)
                session.commit()
                return document
        except DocumentNotFoundError:
            raise
        except (OperationalError, IntegrityError, DBAPIError) as e:
            raise DocumentOperationError(
                f"delete_document failed: id={document_id}, "
                f"user_id={user_id!r}, error={e!s}"
            ) from e
