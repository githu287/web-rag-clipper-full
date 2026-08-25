"""
Document Repository Protocol（Phase 2.9 Step 1；Phase 2.11 Step 2 扩展；
Phase 3.4 Step C user-aware 升级）。

Phase 3.4 Step C 变更总览：
- create_document：user_id 由「可选（默认 None）」升级为「必填」，Repository 不推断；
- get_document(document_id, user_id)：SQL 层 WHERE id + user_id 双条件，
  用户 A 查用户 B 的文档按「不存在」处理（DocumentNotFoundError，不泄露归属）；
- get_documents_by_ids(document_ids, user_id)：SQL 层 IN + user_id 过滤；
- 新增 get_success_document_ids(user_id)：仅返回 id 列表（供后续 RAG 全库隔离）；
- delete_document(document_id, user_id)：SQL 层 WHERE id + user_id，
  用户 A 删用户 B 的文档按「不存在」处理，返回被删 Document；
- update_status / update_ingest_result / update_failure 签名不变：
  调用方必须在前置 get_document(document_id, user_id) ownership check 成功
  后才能操作，document_id 已通过归属校验，不再重复携带 user_id。

【严格阶段约束】
- 仅定义接口（typing.Protocol），不含任何实现代码；
- 不 import sqlalchemy / Engine / Session（具体实现细节在 Impl 中隔离）；
- 不建 engine、不开 session、不执行 SQL；
- 不依赖 FastAPI Depends / Service / API（分层解耦）。

设计风格与 repositories.milvus.protocol.MilvusRepository 完全对齐：
runtime_checkable Protocol + 方法签名 + 行为约束注释，便于上层 Service 用
`repo: DocumentRepository = Depends(get_document_repository)` 注入并支持 Mock。
"""

from __future__ import annotations

from typing import Final, Protocol, runtime_checkable

from ...models.document import Document

# error_message 哨兵值：区分「未传（保持原值不变）」与「显式 None（清空）」。
# 调用方永远不需要显式传 _UNSET，它只作为方法默认值被 Impl 内部判断。
_UNSET: Final[object] = object()


@runtime_checkable
class DocumentRepository(Protocol):
    """
    Document 表数据访问协议（MySQL documents 表 CRUD）。

    实现约束：
    - 不感知 Milvus / Redis / ingest pipeline；
    - 不吞异常：所有 SQLAlchemy 异常统一包装为
      core.exceptions.DocumentRepositoryError 族后向上抛出；
    - engine / sessionmaker 由构造函数注入，禁止在 Impl 内硬编码连接参数；
    - 每个方法 = 一次逻辑 CRUD；不做 Service 级编排。

    Phase 2.11 Step 2 说明（Document 级删除的 Milvus 联动）：
    - 采用方案 A：Document.id == Milvus page_id（1:1）；
    - delete_document 仅负责删除 MySQL documents 行（Repository 单一职责）；
    - Milvus 侧联动（query_page_chunks(page_id) → delete_chunks(ids)）由
      DocumentDeleteService（services/document_delete.py）编排完成，
      不新增 document_id 标量字段、不修改 Milvus Schema。
    """

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
        插入一条 Document 记录。

        新建对象 status 默认 PENDING、chunk_count 默认 0（由 ORM default +
        server_default 双层保证）；created_at / updated_at 由 DB 层
        server_default=func.now() 填充。

        Args:
            filename : 文件名（非空，<=255 字符）。
            file_path: 文件存储路径（非空，<=512 字符）。
            user_id  : 所属用户 ID（**必填**，不允许默认 None；必须由上层认证
                       上下文提供，Repository 不允许自己推断 / 生成 user_id）。
            file_size: 文件字节数（可选；Phase 2.10 Step 3 上传路径传入，
                       None 时保持 ORM 默认值 0，不覆盖既有调用行为）。
            mime_type: 文件 MIME 类型（可选；None 时保持 ORM 默认值 ""）。
            title    : 剪藏标题（可选；Phase 3.1 Step 3 网页剪藏路径传入，
                       None 时写入 NULL）。
            url      : 剪藏来源 URL（可选；None 时写入 NULL）。
            source_type: 来源类型（可选；upload/webpage，None 时保持 ORM
                       默认值 "upload"，既有上传链路调用不受影响）。

        Returns:
            新建的 Document ORM 对象（含自增 id、默认 status/chunk_count、
            DB 填充的 created_at/updated_at）。返回对象为 detached（session
            已 close），因 expire_on_commit=False 属性仍可读。

        Raises:
            DocumentOperationError: SQLAlchemy 执行异常（约束冲突、连接失败等）。
        """

    # -------------------------------------------------------------------- get
    def get_document(self, document_id: int, user_id: int) -> Document:
        """
        按主键 + 归属用户查询 Document（user-aware，Phase 3.4 Step C）。

        SQL：WHERE id = :document_id AND user_id = :user_id。

        隔离语义：用户 A 查询用户 B 的 document_id 时表现为「Document 不存在」，
        抛 DocumentNotFoundError；不得返回 B 的文档、不得泄露「这是别人的文档」。

        Args:
            document_id: Document 主键 ID。
            user_id    : 当前用户 ID（归属过滤条件，由认证上下文提供）。

        Returns:
            Document ORM 对象（detached，属性可读）。

        Raises:
            DocumentNotFoundError: (document_id, user_id) 组合不存在
                （含「文档存在但属于其他用户」的等价语义）。
            DocumentOperationError: SQLAlchemy 执行异常。
        """

    # ----------------------------------------------------------------- get_many
    def get_documents_by_ids(
        self,
        document_ids: list[int],
        user_id: int,
    ) -> list[Document]:
        """
        按主键集合 + 归属用户批量查询 Document（user-aware，Phase 3.4 Step C；
        Phase 2.12 Step 2 起为 RAG status post-filter 专用）。

        供 RagService 在 Milvus 检索后批量反查 Document.status，实现
        「只保留 SUCCESS + 过滤 MySQL 不存在（孤儿 chunk）」的应用层过滤。

        Args:
            document_ids: Document 主键 ID 列表。
            user_id    : 当前用户 ID（归属过滤条件）。

        Returns:
            list[Document]：批量查询结果（detached，属性可读）。
            行为约定：
            - SQL 层直接过滤：WHERE id IN (...) AND user_id = :user_id，
              不允许先查全部再 Python 过滤；
            - document_ids 为空时直接返回 []，不发 SQL；
            - 使用一次批量 IN 查询，禁止逐个调用 get_document()（不 N+1）；
            - 输入中不存在的 ID 直接忽略、不抛 DocumentNotFoundError（调用方
              依赖此语义实现 orphan 过滤）。

        Raises:
            DocumentOperationError: SQLAlchemy 执行异常。
        """

    # ------------------------------------------------------- get_success_ids
    def get_success_document_ids(self, user_id: int) -> list[int]:
        """
        返回指定用户 status == SUCCESS 的 Document id 列表（Phase 3.4 Step C）。

        SQL：SELECT id FROM documents WHERE user_id = :user_id
             AND status = :status_success。

        用途：后续 RAG 全库 user isolation（本步骤只完成 Repository 契约，
        不修改 RagService）。

        要求：
        - 只返回 id（int 列表），不构造完整 Document（省内存，单次 SQL）；
        - 没结果返回 []（不抛异常）；
        - 不允许 Service 拉全部数据再 Python 过滤。

        Args:
            user_id: 当前用户 ID。

        Returns:
            list[int]：该用户全部 SUCCESS 状态的 Document id；无结果时 []。

        Raises:
            DocumentOperationError: SQLAlchemy 执行异常。
        """

    # ----------------------------------------------------------- update_status
    def update_status(
        self,
        doc_id: int,
        status: str,
        *,
        error_message: object = _UNSET,
    ) -> Document:
        """
        更新 Document.status 字段（可选同时更新 error_message）。

        前置校验 status ∈ DocumentStatus.ALL，非法值直接抛 DocumentOperationError
        （不向 DB 发请求，避免无效 UPDATE）。

        error_message 参数采用 sentinel 语义（Phase 2.11 Step 2 扩展）：
        - 不传 error_message：保持原 error_message 不变（向后兼容）；
        - error_message=None：清空 error_message（进入 PROCESSING 时清旧错误）；
        - error_message="xxx"：写入 error_message。
        本方法**不修改 chunk_count**。

        Args:
            doc_id: Document 主键 ID。
            status: 新状态值，必须属于 DocumentStatus.ALL。
            error_message: 可选；None=清空，str=写入，缺省=不修改。

        Returns:
            更新后的 Document ORM 对象（已 commit + refresh，detached 可读）。

        Raises:
            DocumentNotFoundError: doc_id 不存在。
            DocumentOperationError: status 非法或 SQLAlchemy 执行异常。
        """

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
        （可选同时更新 error_message）。

        供 DocumentIngestService 成功路径调用（Phase 2.9 Step 2）：
        统一使用本方法完成 `chunk_count + status=SUCCESS` 的原子提交，
        禁止先 update_ingest_result 再 update_status(SUCCESS) 两步拆分。

        error_message 参数采用 sentinel 语义（Phase 2.11 Step 2 扩展）：
        - 不传 error_message：保持原 error_message 不变（向后兼容）；
        - error_message=None：清空 error_message（成功路径清空旧错误）；
        - error_message="xxx"：写入 error_message。

        Args:
            document_id: Document 主键 ID（1:1 对应 Milvus page_id，方案 A）。
            chunk_count: 本次 ingest 成功后写入的 chunk 总数（= len(chunks)）。
            status: 新状态值，必须属于 DocumentStatus.ALL（通常为 SUCCESS）。
            error_message: 可选；None=清空，str=写入，缺省=不修改。

        Returns:
            更新后的 Document ORM 对象（已 commit + refresh，detached 可读）。

        Raises:
            DocumentNotFoundError: document_id 不存在。
            DocumentOperationError: status 非法或 SQLAlchemy 执行异常。
        """

    # ------------------------------------------------------------ update_failure
    def update_failure(
        self,
        document_id: int,
        *,
        error_message: str,
    ) -> Document:
        """
        在**同一个数据库事务**中一次更新 Document.status=FAILED 与 error_message。

        供 DocumentUploadService 失败路径调用（Phase 2.10 Step 3）：
        在 Parser / Chunker / ingest 任一环节失败后，以本方法原子落
        `status=FAILED + error_message=截断摘要`。

        本方法**不修改 chunk_count**（保持创建时的 0），保证失败路径
        不会把「当前已切出的 chunks 数量」误写入 chunk_count。

        Args:
            document_id : Document 主键 ID（1:1 对应 Milvus page_id）。
            error_message: 错误摘要（调用方负责截断到可控长度；必填，非空）。

        Returns:
            更新后的 Document ORM 对象（已 commit + refresh，detached 可读）。

        Raises:
            DocumentNotFoundError: document_id 不存在。
            DocumentOperationError: SQLAlchemy 执行异常。
        """

    # ------------------------------------------------------------------ delete
    def delete_document(self, document_id: int, user_id: int) -> Document:
        """
        按主键 + 归属用户删除 Document 记录（user-aware，仅删 MySQL documents 行）。

        SQL：WHERE id = :document_id AND user_id = :user_id。

        隔离语义：用户 A 删除用户 B 的 document 时按「不存在」处理，抛
        DocumentNotFoundError；不得删除 B 的文档、不泄露归属信息。

        【范围边界】本方法只负责 MySQL 侧行删除（Repository 单一职责）；
        Milvus chunks 与 FileStorage 物理文件的联动删除由
        DocumentDeleteService（services/document_delete.py）编排，不在本方法内
        执行；Milvus → FileStorage → MySQL 的删除顺序由 DeleteService 保证，
        本步骤不改变。

        Args:
            document_id: Document 主键 ID。
            user_id    : 当前用户 ID（归属过滤条件）。

        Returns:
            被删除的 Document ORM 对象（detached，属性可读；DB 行已删除）。

        Raises:
            DocumentNotFoundError: (document_id, user_id) 组合不存在（删除 0 行）。
            DocumentOperationError: SQLAlchemy 执行异常。
        """
