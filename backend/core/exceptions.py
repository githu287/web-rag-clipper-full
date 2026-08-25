"""
Repository 层异常契约。

本模块定义两个异常族，二者相互独立、无继承关系：

1) Milvus Repository 异常族（与 Phase 2.3 §8 完全一致）：
   - MilvusRepositoryError        → 根类
   - MilvusConnectionError        → 可重试的连接/超时/网络错
   - MilvusOperationError         → Milvus 执行错误（集合不存在/索引未就绪/内部错）
   - MilvusSchemaMismatchError    → 不可重试的契约错（维度/字段/字节长度/id 不一致；
                                      Phase 2.3 经验库 610470 防御）

2) Document Repository 异常族（Phase 2.9 Step 1 新增，MySQL documents 表）：
   - DocumentRepositoryError      → 根类（包装 SQLAlchemy 原生异常）
   - DocumentNotFoundError       → 主键不存在（get/delete/update 返回 0 行）
   - DocumentOperationError      → 操作失败（连接/约束/字段等 DB 执行错）

通用设计原则：
1) Repository 不吞异常：所有底层错误统一封装为对应族异常后抛给 Service 层，由 Service 决定
   任务内重试 / 转状态 FAILED / 抛 API 500。
2) 异常链保留：Impl 必须使用 `raise XxxError(...) from original_exception`（Python 异常链）
   保留底层原因，便于日志快速定位。
"""

from __future__ import annotations


class MilvusRepositoryError(Exception):
    """
    Milvus Repository 异常根类。

    Service 层可以用：
        try:
            repo.upsert_chunks(...)
        except MilvusRepositoryError:
            # 统一兜底（重试 or 转 FAILED）
            ...
    """


class MilvusConnectionError(MilvusRepositoryError):
    """
    连接类失败：连接超时、连接拒绝、DNS 解析失败、gRPC transport 断开、鉴权失败（若启用）。

    通常可由 Service 层任务内指数退避重试。
    实现期建议：捕获 pymilvus 连接相关异常后，以 `raise MilvusConnectionError(...) from e` 方式抛出，
    保留原始堆栈与根因。
    """


class MilvusOperationError(MilvusRepositoryError):
    """
    Milvus 操作类失败：参数合法但 Milvus 执行时失败（Collection 不存在、索引未就绪、
    节点内部错误、delete/upsert 返回 error_cnt>0、query/search 返回非预期字段集）。

    Service 层按场景判断是否重试或直接标记 Document FAILED。
    """


class MilvusSchemaMismatchError(MilvusRepositoryError):
    """
    数据契约不一致异常（Phase 2.3 §8 定义的不可重试契约错；与经验库 ID 610470「三点闭环」
    「DTO 字段集 ≡ Milvus Schema 字段集」失败时抛出）。

    典型触发场景（Impl 期二次防御或 DTO 校验失败均应抛该类）：
      ① search vector len != 1024（与 Milvus embedding.dim / BAILIAN_EMBEDDING_DIMENSION 不一致）
      ② upsert 某 ChunkVector.embedding len != 1024
      ③ ChunkVector.chunk_text UTF-8 字节长度 > 4096（VARCHAR(4096) 最大字节）
      ④ ChunkVector.id 与 page_id/chunk_index 推导出的 f"{pid}_{cid}" 不一致
      ⑤ Impl 返回的 ChunkSearchResult 字段缺失或包含 embedding（Phase 2.2 §13.1 禁止）

    处理策略：**不可重试**（重试只会重复报同样的契约错误）；Service 层应立即标记 FAILED
    并记录完整 error_message 方便修复配置/代码。
    """


# ============================================================================
# Document Repository 异常族（Phase 2.9 Step 1 新增）
# ============================================================================


class DocumentRepositoryError(Exception):
    """
    Document Repository 异常根类（MySQL documents 表数据访问层）。

    设计与 MilvusRepositoryError 族对齐：Repository 不吞异常，所有 SQLAlchemy
    原生异常（OperationalError / IntegrityError / DBAPIError 等）由 Impl 统一
    包装为本族异常后抛给 Service 层，由 Service 决定重试 / 转 FAILED / 抛 API 500。

    异常链保留：Impl 必须使用 `raise DocumentRepositoryError(...) from e`，
    保留底层 SQLAlchemy / PyMySQL 原因，便于日志定位。
    """


class DocumentNotFoundError(DocumentRepositoryError):
    """
    指定主键的 Document 不存在。

    触发场景：
      - get_document(doc_id) 查询返回 None
      - update_status / delete_document 操作 0 行（即 doc_id 不在表中）

    处理策略：不可重试（重试仍是不存在）。调用方（Service / API）应映射为 404。
    """


class DocumentOperationError(DocumentRepositoryError):
    """
    Document 表操作类失败：SQL 执行异常（连接超时、约束冲突、字段不匹配等）。

    与 MilvusOperationError 语义对齐：参数合法但 DB 执行时失败。
    Service 层按具体异常判断是否重试或直接转 FAILED。

    典型包装场景（Impl 内 try/except 捕获后包装）：
      - sqlalchemy.exc.OperationalError  → 连接/超时/死锁等（可重试）
      - sqlalchemy.exc.IntegrityError    → 唯一约束 / 外键冲突（不可重试）
      - sqlalchemy.exc.DBAPIError        → 其他底层 DBAPI 错误
    """


class DocumentNotSuccessError(DocumentRepositoryError):
    """
    Document 存在但状态不是 SUCCESS（Phase 3.3 Step 3 新增）。

    触发场景：RagAnswerService.ask 指定 document_id 且该 Document 状态不是 SUCCESS
    （PENDING / PROCESSING / FAILED / DELETING）时抛出，表示不允许基于该文档回答。

    处理策略：不可重试（重试仍不是 SUCCESS）。调用方应映射为 409 Conflict。
    """


# ============================================================================
# Document 文件处理异常族（Phase 2.10 Step 2 新增：storage / parser / chunker 三层）
# ============================================================================


class DocumentStorageError(Exception):
    """
    FileStorage 层异常根类（原始文件保存 / 删除 / 路径解析）。

    触发场景：
      - 文件名不安全（路径穿越 `../../x.txt`、空文件名、非法分隔符）
      - 目标路径解析后超出 upload_dir 边界
      - 磁盘写入 / 删除失败（权限、IO 错误等）

    设计：不吞异常，Impl 以 `raise DocumentStorageError(...) from e` 保留底层根因。
    """


class DocumentStoragePathTraversalError(DocumentStorageError):
    """
    文件名 / 路径存在穿越风险（Phase 2.10 Step 2 安全红线）。

    触发场景：save/delete 收到的 filename/file_path 经解析后位于 upload_dir 之外，
    或文件名包含 `..` 等路径穿越片段。此类错误不可重试（输入本身非法），
    调用方应拒绝请求而非修正后重试。
    """


class DocumentParserError(Exception):
    """
    Parser 层异常根类（文件 → 完整文本）。

    触发场景：
      - 文件扩展名不受支持（.pdf / .docx 等本阶段未实现格式）
      - 文件不存在 / 不可读（OSError / FileNotFoundError / 权限）
      - 编码解码失败（UTF-8 / 回退编码均失败）

    设计：Parser 只做「文件 → str」，禁止调用 Embedding / Milvus / MySQL；
    读取失败以 `raise DocumentParserError(...) from e` 保留根因。
    """


class DocumentParserUnsupportedExtensionError(DocumentParserError):
    """
    文件扩展名不受支持。

    本阶段（Phase 2.10 Step 2）仅支持 .txt / .md / .markdown；
    .pdf / .docx / .html 等抛出本异常，由上层明确告知用户格式不支持。
    不可重试。
    """


class DocumentParserReadError(DocumentParserError):
    """
    文件读取失败：不存在 / 无权限 / 编码不兼容等。

    可能根因：文件被删除、磁盘 IO 错误、UTF-8 与回退编码均解码失败。
    可重试性取决于根因（文件不存在不可重试，IO 抖动可重试）。
    """


class DocumentChunkingError(Exception):
    """
    Chunker 层异常根类（完整文本 → 切分块列表）。

    触发场景：
      - chunk_size / chunk_overlap 配置非法（chunk_size < 1、chunk_overlap < 0、
        chunk_overlap >= chunk_size）
      - 切分过程中发现不可恢复的内部状态错误

    设计：Chunker 只做「str → list[str]」，禁止调用 Embedding / Milvus / MySQL。
    """


class DocumentChunkingConfigError(DocumentChunkingError):
    """
    Chunker 配置非法。

    触发场景：chunk_size >= 1 且 chunk_overlap >= 0 且 chunk_overlap < chunk_size
    约束不满足（例如 overlap >= chunk_size）。
    不可重试，属于配置缺陷，应修配置后重启。
    """


# ============================================================================
# Document Upload 异常族（Phase 2.10 Step 3 新增：上传入口业务校验）
# ============================================================================


class DocumentUploadError(Exception):
    """
    Document 上传流程异常根类（HTTP 入口 → DocumentUploadService 入口校验）。

    触发场景（在 FileStorage.save() 与创建 Document 之前发生的输入校验失败）：
      - 文件名非法（空文件名、包含路径分隔符）
      - 文件内容为空
      - 文件超过 max_page_content_bytes 上限
      - 扩展名不属于本阶段支持集合（.txt / .md / .markdown）

    设计：此类错误**不可重试**（输入本身非法），且发生在落盘/建 Document 之前，
    不产生任何 Document 行；API 层映射为 4xx（400 / 413 / 415）。
    不吞异常：Service 内业务校验直接抛出本族异常，不包装底层根因。
    """


class DocumentFileTooLargeError(DocumentUploadError):
    """
    上传文件超过系统上限（settings.max_page_content_bytes）。

    触发场景：len(content) > max_page_content_bytes。
    不可重试（换更大上限或缩小文件），API 映射 413。
    """


class DocumentFileEmptyError(DocumentUploadError):
    """
    上传文件内容为空（0 字节）。

    触发场景：content == b""。
    不可重试，API 映射 400；禁止以空内容生成 0 chunk 的 SUCCESS Document。
    """


class DocumentUnsupportedExtensionError(DocumentUploadError):
    """
    上传文件扩展名不受支持。

    本阶段（Phase 2.10 Step 3）仅支持 .txt / .md / .markdown；
    .pdf / .docx 等在 UploadService 入口提前拒绝，不进入 Parser。
    不可重试，API 映射 415。
    """


# ============================================================================
# Security 异常族（Phase 3.4 Step 3 新增：纯安全工具模块 core.security）
# ============================================================================


class SecurityError(Exception):
    """
    Security 工具异常根类（backend/core/security.py）。

    覆盖 sha256 / token 生成 / AES-256-GCM 加解密过程的全部失败。
    与 Repository 异常族独立：Security 是纯函数模块，不依赖 DB / Service。
    """


class SecurityConfigurationError(SecurityError):
    """
    安全配置错误：APP_MASTER_KEY 非 32 bytes（AES-256 密钥长度）等。

    不可重试（配置缺陷），修配置后重启。
    """


class SecurityDecryptionError(SecurityError):
    """
    解密失败：密文 / nonce Base64 损坏、GCM authentication tag 验证失败、
    密钥错误（wrong master key / 篡改密文）。

    不可重试（输入或密钥错误），错误消息不包含任何明文 / 密文内容。
    """


# ============================================================================
# User Repository 异常族（Phase 3.4 Step 3 新增：MySQL users 表数据访问层）
# ============================================================================


class UserRepositoryError(Exception):
    """
    User Repository 异常根类（MySQL users 表数据访问层）。

    设计与 DocumentRepositoryError 族对齐：Repository 不吞异常，所有 SQLAlchemy
    原生异常（OperationalError / IntegrityError / DBAPIError 等）由 Impl 统一
    包装为本族异常后抛给上层，由 Service 决定重试 / 抛 API 错误。
    """


class UserNotFoundError(UserRepositoryError):
    """
    指定 user_id 的 User 不存在（update_api_key 更新 0 行）。

    触发场景：update_api_key(user_id=...) 查询返回 None。
    不可重试（重试仍是不存在），调用方应映射 404。

    查询方法（get_user_by_token_hash / get_user_by_api_key_hash / get_user_by_id）
    查不到返回 None，不抛本异常（认证/注册幂等场景由 Service 决定 401/404）。
    """


class UserOperationError(UserRepositoryError):
    """
    users 表操作类失败：SQL 执行异常（连接超时、唯一约束冲突、字段不匹配等）。

    与 DocumentOperationError 语义对齐：参数合法但 DB 执行时失败。
    典型包装场景（Impl 内 try/except 捕获后包装）：
      - sqlalchemy.exc.OperationalError  → 连接/超时/死锁等（可重试）
      - sqlalchemy.exc.IntegrityError    → unique(api_key_hash / token_hash) 冲突（不可重试）
      - sqlalchemy.exc.DBAPIError        → 其他底层 DBAPI 错误
    """


# ============================================================================
# Auth 异常族（Phase 3.4 Step 4 新增：认证 / 用户身份接入 / API Key 注入）
# ============================================================================


class AuthError(Exception):
    """
    认证 / 授权异常根类（backend/services/user_service.py + backend/api/deps.py）。

    设计与 Security / Repository 异常族独立：
    - Auth 异常承载「请求者身份」语义（未认证 / 未授权 / 冲突），
      HTTP 状态码映射由 API 层（main.py 全局 handler）负责；
    - 错误消息不包含任何明文 API Key / token。
    """


class AuthenticationError(AuthError):
    """
    认证失败：Bearer token 缺失 / 无效 / 已过期 / 用户被禁用。

    触发场景（backend/api/deps.py get_current_user）：
      - 请求头无 Authorization 或非 "Bearer <token>" 格式；
      - token 对应的 token_hash 在 users 表查不到（无效 / 已过期）；
      - 用户 status != ACTIVE（DISABLED，不允许使用业务 API）。

    处理策略：不可重试（客户端需重新注册 / 登录获取有效 token），映射 401。
    """


class ApiKeyInvalidError(AuthError):
    """
    API Key 无效：登录（/auth/login）时该 API Key 未注册 / 已禁用。

    触发场景（UserService.login）：
      - get_user_by_api_key_hash 返回 None（未注册，应先 /auth/register）。

    处理策略：不可重试（输入本身无效），映射 401；
    与「跨用户访问他人文档 → 404」原则一致——不泄露任何已存在信息。
    """


class ApiKeyAlreadyRegisteredError(AuthError):
    """
    API Key 已注册：注册（/auth/register）时该 API Key 已存在。

    触发场景（UserService.register）：
      - get_user_by_api_key_hash 返回非 None（该 API Key 已注册过）。

    处理策略：不可重试（客户端应改用 /auth/login），映射 409 Conflict；
    提示信息引导用户直接登录，不泄露用户身份细节。
    """


class AuthOperationError(AuthError):
    """
    认证服务内部错误：UserRepository 操作失败（UserOperationError 包装转发）。

    触发场景（UserService 调用 create_user / get_user_by_* / update_api_key /
    update_token 时 UserRepository 抛出 UserOperationError）。

    处理策略：外部数据服务问题（MySQL 连接 / 约束冲突），映射 503 Service
    Unavailable，与 DocumentOperationError → 503 风格对齐。
    """
