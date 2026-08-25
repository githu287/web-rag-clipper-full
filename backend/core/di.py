"""
依赖注入装配中心（Phase 2.6 Step 3 扩展；Phase 2.9 Step 1 增 Document Repository）。

职责：
1) 提供 Settings 单例（get_settings，Phase 2.4 Step 1 已实现）。
2) 提供 MilvusRepository（Protocol）实例工厂 get_milvus_repository()：
   - 返回类型为 Protocol（MilvusRepository），上层 Service 不直接依赖 PyMilvusRepositoryImpl。
3) 提供 MilvusInitializer 实例工厂 get_milvus_initializer()：
   - 仅对象装配；**不在 DI 层调用 initialize()**（生命周期由上层启动钩子/管理命令触发）。
4) 提供 EmbeddingClient 实例工厂 get_embedding_client()（Phase 2.5 Step 3 新增）：
   - 仅对象装配；**不在 DI 层创建 OpenAI Client / 调用 embed()**（连接由 embed() 内部惰性建立）。
5) 提供 IngestService 实例工厂 get_ingest_service()（Phase 2.6 Step 3 新增）：
   - 装配 EmbeddingClient + MilvusRepository → IngestService；不调用 ingest_page()。
6) 提供 RagService 实例工厂 get_rag_service()（Phase 2.6 Step 3 新增）：
   - 装配 EmbeddingClient + MilvusRepository → RagService；不调用 search()。
7) 提供 DocumentRepository（Protocol）实例工厂 get_document_repository()（Phase 2.9 Step 1 新增）：
   - 装配 Settings → Engine → DocumentRepositoryImpl；不调用 CRUD 方法。
8) 提供 LLMClient（Protocol）实例工厂 get_llm_client()（Phase 3.3 Step 3 新增）：
   - 装配 Settings → BailianLLMClient；不调用 generate() / 不创建 OpenAI Client。
9) 提供 RagAnswerService 实例工厂 get_rag_answer_service()（Phase 3.3 Step 3 新增）：
   - 装配 RagService + LLMClient + DocumentRepository → RagAnswerService；不调用 ask()。
10) 提供 UserRepository（Protocol）实例工厂 get_user_repository()（Phase 3.4 Step 4 新增）：
    - 装配 Engine → UserRepositoryImpl；不调用 CRUD 方法。
11) 提供 UserService 实例工厂 get_user_service()（Phase 3.4 Step 4 新增）：
    - 装配 UserRepository + Settings(app_master_key) → UserService；不调用
      register / login / update_api_key / decrypt_api_key。

DI 层允许装配：
- Milvus Repository
- Milvus Initializer
- Embedding Client
- LLM Client（Phase 3.3 Step 3 新增）
- IngestService（业务编排）
- RagService（业务编排）
- RagAnswerService（业务编排，Phase 3.3 Step 3 新增）
- DocumentRepository（MySQL documents 表 CRUD，Phase 2.9 Step 1 新增）
- UserRepository（MySQL users 表 CRUD，Phase 3.4 Step 4 新增）
- UserService（用户认证业务编排，Phase 3.4 Step 4 新增）

但 DI 层禁止：
- 建立外部连接（Milvus / 百炼 OpenAI 兼容 API / MySQL）
- 调用初始化方法（如 MilvusInitializer.initialize()）
- 执行业务逻辑（如 embed() / search() / upsert() / ingest_page() / create_document()）

经验库 153832 规则应用：
- 配置单源：所有工厂统一从 get_settings() 取 Settings，不在 DI 层重复解析 .env；
- 延迟真实连接：DI 仅返回对象实例，对象内部所有外部连接均在方法调用时建立，
  避免「import 阶段 / 构造阶段」对 Milvus / 百炼 / MySQL 服务的硬依赖，做到「无外部服务也能 import」。
- 单向依赖：di → services → clients/repositories → core.config/exceptions/models；
  services 不反向 import core.di，保证无循环依赖。
"""

from __future__ import annotations

from functools import lru_cache

from ..chunkers import Chunker, RecursiveCharacterChunker
from ..clients.embedding import EmbeddingClient
from ..clients.llm import BailianLLMClient, LLMClient
from ..parsers import DocumentParser, TextDocumentParser
from ..repositories.milvus import (
    MilvusInitializer,
    MilvusRepository,
    PyMilvusRepositoryImpl,
)
from ..repositories.mysql import (
    DocumentRepository,
    DocumentRepositoryImpl,
    UserRepository,
    UserRepositoryImpl,
)
from ..services.document_delete import DocumentDeleteService
from ..services.document_ingest import DocumentIngestService
from ..services.document_upload import DocumentUploadService
from ..services.ingest import IngestService
from ..services.rag import RagService
from ..services.rag_answer import RagAnswerService
from ..services.user_service import UserService
from ..services.web_clip import WebClipService
from ..storage import FileStorage, LocalFileStorage
from .config import Settings, get_default_settings
from .db import get_engine


def get_settings() -> Settings:
    """
    Settings 依赖入口：返回进程内缓存的 Settings 单例。

    设计目的：
    1) 上层代码（包括后续 Impl/Service/FastAPI Depends）统一从此函数拿配置，避免重复解析 .env；
    2) 后续如果需要支持测试环境覆盖，只需 monkey-patch 环境变量 + 调用 `get_default_settings.cache_clear()`。
    """
    return get_default_settings()


@lru_cache(maxsize=1)
def get_milvus_repository() -> MilvusRepository:
    """
    返回 MilvusRepository（Protocol）实例。

    依赖链：
        Settings (get_settings)
            ↓
        PyMilvusRepositoryImpl(settings)
            ↓
        以 MilvusRepository Protocol 类型返回

    设计要点（经验库 153832 + Phase 2.3 §2.2「Service 不直接依赖 Impl」）：
    1) **返回类型为 Protocol**：上层 Service 用 `repo: MilvusRepository = Depends(get_milvus_repository)`
       而非 `PyMilvusRepositoryImpl`，从而：
         - Service 单元测试可注入 Mock 实现（同 Protocol，runtime_checkable 校验通过）；
         - 未来若新增第二种实现（如 AsyncImpl / TestImpl），只需在此工厂切换，Service 代码零改动；
         - 编译期约束：Service 无法访问 Impl 的私有方法/属性，避免越权。
    2) **lru_cache 单例**：进程内只构造一次 Impl 实例，避免每次 Depends 都重建对象
       （Impl 内部本就延迟建 MilvusClient，单例化不会带来连接泄漏）。
    3) **不在 import 时连接 Milvus**：Impl.__init__ 仅读取 Settings 字段，不打开连接；
       真实 MilvusClient 由 Impl 方法内部 `with self._make_client() as client:` 按需建立。

    Returns:
        MilvusRepository: Protocol 类型实例（实际为 PyMilvusRepositoryImpl，但 Service 不感知具体类型）。
    """
    settings = get_settings()
    return PyMilvusRepositoryImpl(settings)


@lru_cache(maxsize=1)
def get_milvus_initializer() -> MilvusInitializer:
    """
    返回 MilvusInitializer 实例（仅对象装配，不触发 initialize()）。

    依赖链：
        Settings (get_settings)
            ↓
        MilvusInitializer(settings)

    设计要点：
    1) **DI 不调用 initialize()**：Collection 创建属于「启动期一次性副作用」，
       应由 FastAPI lifespan / 管理命令 / 启动脚本显式触发，而不是在 DI 工厂里隐式执行；
       这样可以保证：
         - 单元测试 import 本模块时不会真的去连 Milvus；
         - 启动失败可以由上层决定重试策略 / 转入降级模式。
    2) **lru_cache 单例**：进程内共享一个 Initializer 实例（幂等，多次 initialize 也安全）。
    3) **不在 import 时连接 Milvus**：MilvusInitializer.__init__ 仅保存 Settings，
       真实 MilvusClient 由 initialize() 内部 `with MilvusClient(...) as client:` 按需建立。

    Returns:
        MilvusInitializer: 待触发的初始化器实例（调用方需自行调用 .initialize()）。
    """
    settings = get_settings()
    return MilvusInitializer(settings)


@lru_cache(maxsize=1)
def get_embedding_client() -> EmbeddingClient:
    """
    返回 EmbeddingClient 单例（Phase 2.5 Step 3 新增）。

    依赖链：
        Settings (get_settings)
            ↓
        EmbeddingClient(settings)
            ↓
        以 EmbeddingClient 类型返回

    只负责依赖装配：
    - 获取 Settings 单例
    - 创建 EmbeddingClient 实例（仅保存 settings 引用）

    不执行（生命周期红线）：
    - OpenAI Client 创建（由 EmbeddingClient._get_client() 在首次 embed() 时惰性创建）
    - embed() 调用（由 Service 层按需触发）
    - 网络请求 / API 初始化

    设计要点：
    1) **lru_cache 单例**：进程内只构造一次 EmbeddingClient 实例（EmbeddingClient 内部
       OpenAI client 也是惰性单例，DI 单例化不会带来连接泄漏）。
    2) **不在 import 时连接百炼**：EmbeddingClient.__init__ 仅保存 Settings 字段引用，
       不打开 OpenAI 连接；真实 OpenAI client 由 _get_client() 在首次 embed() 调用时建立。
    3) **配置单源**：API Key / base_url / model / dimension / batch_size 全部从 Settings 注入，
       DI 层不重复解析 .env（经验库 153832）。

    Returns:
        EmbeddingClient: 待调用的嵌入客户端实例（调用方需自行调用 .embed(texts)）。
    """
    settings = get_default_settings()
    return EmbeddingClient(settings)


@lru_cache(maxsize=1)
def get_ingest_service() -> IngestService:
    """
    返回 IngestService 单例（Phase 2.6 Step 3 新增）。

    依赖链：
        get_embedding_client()   → EmbeddingClient
        get_milvus_repository()  → MilvusRepository (Protocol)
            ↓
        IngestService(embedding_client, milvus_repository)

    只负责依赖装配：
    - 获取已缓存的 EmbeddingClient 单例
    - 获取已缓存的 MilvusRepository（Protocol）单例
    - 创建 IngestService 实例（构造函数仅保存两个依赖引用）

    不执行（生命周期红线）：
    - ingest_page() 调用（由 API 层 / 管理命令按需触发）
    - embed() / upsert_chunks() / delete_chunks() / query_page_chunks() 调用
    - 任何 Milvus / 百炼网络请求

    设计要点：
    1) **lru_cache 单例**：IngestService 无状态（仅持有两个注入依赖的引用），
       进程内共享一个实例安全且高效，与已有 DI 工厂风格一致。
    2) **依赖复用**：EmbeddingClient 与 MilvusRepository 均为 lru_cache 单例，
       IngestService 与 RagService 共享同一对底层实例，不会重复创建连接池。
    3) **Protocol 注入**：传入的 milvus_repository 类型为 MilvusRepository Protocol，
       IngestService 不感知 PyMilvusRepositoryImpl 具体类型，便于单元测试 Mock。
    4) **不调用业务方法**：DI 仅 return 对象，不触发 re-ingest 三步流程。

    Returns:
        IngestService: 待调用的入库编排服务实例（调用方需自行 await .ingest_page()）。
    """
    embedding_client = get_embedding_client()
    milvus_repository = get_milvus_repository()
    return IngestService(embedding_client, milvus_repository)


@lru_cache(maxsize=1)
def get_rag_service() -> RagService:
    """
    返回 RagService 单例（Phase 2.6 Step 3 新增；Phase 2.12 Step 2 接入 DocumentRepository）。

    依赖链：
        get_embedding_client()      → EmbeddingClient
        get_milvus_repository()     → MilvusRepository (Protocol)
        get_document_repository()   → DocumentRepository (Protocol)
            ↓
        RagService(embedding_client, milvus_repository, document_repository)

    只负责依赖装配：
    - 获取已缓存的 EmbeddingClient 单例
    - 获取已缓存的 MilvusRepository（Protocol）单例
    - 获取已缓存的 DocumentRepository（Protocol）单例
    - 创建 RagService 实例（构造函数仅保存三个依赖引用）

    不执行（生命周期红线）：
    - search() 调用（由 API 层按需触发）
    - embed() / Milvus search / get_documents_by_ids 调用
    - 任何 Milvus / 百炼 / MySQL 网络请求
      （get_document_repository 内部 get_engine 用 lru_cache 惰性建立，
        create_engine 本身不立即开 TCP 连接）

    设计要点：
    1) **lru_cache 单例**：RagService 无状态，进程内共享一个实例安全且高效。
    2) **依赖复用**：与 IngestService / DocumentIngestService 共享 EmbeddingClient +
       MilvusRepository + DocumentRepository 单例，不会重复创建连接池。
    3) **Protocol 注入**：milvus_repository / document_repository 均为 Protocol 类型，
       RagService 不感知具体 Impl，便于单元测试 Mock。
    4) **不调用业务方法**：DI 仅 return 对象，不触发 RAG 检索流程。

    Returns:
        RagService: 待调用的 RAG 检索服务实例（调用方需自行 await .search()）。
    """
    embedding_client = get_embedding_client()
    milvus_repository = get_milvus_repository()
    document_repository = get_document_repository()
    return RagService(embedding_client, milvus_repository, document_repository)


@lru_cache(maxsize=1)
def get_document_repository() -> DocumentRepository:
    """
    返回 DocumentRepository（Protocol）实例（Phase 2.9 Step 1 新增）。

    依赖链：
        Settings (get_settings)
            ↓
        Engine (core.db.get_engine)
            ↓
        DocumentRepositoryImpl(engine)
            ↓
        以 DocumentRepository Protocol 类型返回

    只负责依赖装配：
    - 获取 Settings 单例
    - 通过 core.db.get_engine 获取 MySQL Engine 单例（lru_cache 进程内唯一）
    - 创建 DocumentRepositoryImpl 实例（构造函数仅保存 engine 引用 + 建 sessionmaker）

    不执行（生命周期红线）：
    - 任何 SQL 执行（create_document / get_document / update_status / delete_document）
    - MySQL TCP 连接（create_engine 本身不开连接，真实连接在首次 SQL 执行时建立）

    设计要点：
    1) **返回类型为 Protocol**：上层 Service 用
       `repo: DocumentRepository = Depends(get_document_repository)` 而非
       DocumentRepositoryImpl，从而：
         - 单元测试可注入 Mock 实现（同 Protocol，runtime_checkable 校验通过）；
         - 未来若新增 AsyncImpl / TestImpl，只需在此工厂切换，Service 代码零改动；
         - 编译期约束：Service 无法访问 Impl 的私有方法/属性，避免越权。
    2) **注入 Engine（而非 Settings）**：Impl 接受 Engine，便于单元测试用
       SQLite in-memory engine 替换（StaticPool），不依赖真实 MySQL。
    3) **lru_cache 单例**：进程内只构造一次 Impl 实例（内部 sessionmaker 共享）；
       get_engine 也是 lru_cache 单例，Engine 与 sessionmaker 不会重复创建。
    4) **不在 import 时连接 MySQL**：get_engine 用 lru_cache 惰性建立，
       create_engine 本身不立即开连接，真实 TCP 连接在首次 SQL 执行时建立，
       故 import 本模块 / 调用本函数不会因 MySQL 不可达而失败。

    Returns:
        DocumentRepository: Protocol 类型实例（实际为 DocumentRepositoryImpl，
        但 Service 不感知具体类型）。
    """
    engine = get_engine()
    return DocumentRepositoryImpl(engine)


@lru_cache(maxsize=1)
def get_document_ingest_service() -> DocumentIngestService:
    """
    返回 DocumentIngestService 单例（Phase 2.9 Step 2 新增）。

    依赖链：
        get_document_repository() → DocumentRepository (Protocol)
        get_ingest_service()      → IngestService
            ↓
        DocumentIngestService(document_repository, ingest_service)

    只负责依赖装配：
    - 获取已缓存的 DocumentRepository（Protocol）单例
    - 获取已缓存的 IngestService 单例
    - 创建 DocumentIngestService 实例（构造函数仅保存两个依赖引用）

    不执行（生命周期红线）：
    - get_document / update_status / update_ingest_result 调用
    - ingest_page() 调用
    - 任何 MySQL / Milvus / 百炼网络请求

    设计要点：
    1) **lru_cache 单例**：DocumentIngestService 无状态（仅持有两个注入依赖的引用），
       进程内共享一个实例安全且高效，与已有 DI 工厂风格一致。
    2) **依赖复用**：DocumentRepository 与 IngestService 均为 lru_cache 单例，
       不会重复创建 Engine / sessionmaker / Milvus 连接池。
    3) **Protocol 注入**：document_repository 类型为 DocumentRepository Protocol，
       DocumentIngestService 不感知 DocumentRepositoryImpl 具体类型，便于单元测试 Mock。
    4) **不调用业务方法**：DI 仅 return 对象，不触发 ingest 链路。

    Returns:
        DocumentIngestService: 待调用的 Document 生命周期编排服务实例
        （调用方需自行 await .ingest_document()）。
    """
    document_repository = get_document_repository()
    ingest_service = get_ingest_service()
    return DocumentIngestService(document_repository, ingest_service)


@lru_cache(maxsize=1)
def get_file_storage() -> FileStorage:
    """
    返回 FileStorage（Protocol）实例（Phase 2.10 Step 2 新增）。

    依赖链：
        Settings (get_settings)
            ↓
        LocalFileStorage(settings.upload_dir)
            ↓
        以 FileStorage Protocol 类型返回

    设计要点：
    1) **返回类型为 Protocol**：上层通过 `storage: FileStorage = Depends(...)`
       注入，便于测试 Mock 与未来切换对象存储实现。
    2) **lru_cache 单例**：进程内共享一个 LocalFileStorage（无状态，仅存目录）。
    3) **不在 import / 构造时连接任何外部服务**：LocalFileStorage 仅存 upload_dir，
       不连接 MinIO / S3；目录在首次 save() 时创建。
    4) **不执行业务方法**：DI 仅 return 对象，不 save/delete。

    Returns:
        FileStorage: 待调用的文件存储实例（调用方需自行调用 save/delete）。
    """
    settings = get_settings()
    return LocalFileStorage(settings.upload_dir)


@lru_cache(maxsize=1)
def get_text_parser() -> DocumentParser:
    """
    返回 DocumentParser（Protocol）实例（Phase 2.10 Step 2 新增）。

    依赖链：
        TextDocumentParser()
            ↓
        以 DocumentParser Protocol 类型返回

    设计要点：
    1) **返回类型为 Protocol**：上层通过 `parser: DocumentParser = Depends(...)`
       注入，便于测试 Mock 与未来扩展 PDFParser/DocxParser。
    2) **lru_cache 单例**：TextDocumentParser 无状态（无构造参数）。
    3) **不连接外部服务**：Parser 仅操作本地文件系统，不调用 Embedding / Milvus / MySQL。

    Returns:
        DocumentParser: 待调用的解析器实例（调用方需自行调用 .parse(file_path)）。
    """
    return TextDocumentParser()


@lru_cache(maxsize=1)
def get_chunker() -> Chunker:
    """
    返回 Chunker（Protocol）实例（Phase 2.10 Step 2 新增）。

    依赖链：
        Settings (get_settings)
            ↓
        RecursiveCharacterChunker(chunk_size, chunk_overlap)
            ↓
        以 Chunker Protocol 类型返回

    设计要点：
    1) **返回类型为 Protocol**：上层通过 `chunker: Chunker = Depends(...)`
       注入，便于测试 Mock 与未来扩展语义切分实现。
    2) **配置单源**：chunk_size / chunk_overlap 从 Settings 注入
       （默认 700 / 100，与 .env.example 的 CHUNK_SIZE / CHUNK_OVERLAP 对齐）。
    3) **lru_cache 单例**：RecursiveCharacterChunker 无状态（仅存参数）。
    4) **不连接外部服务**：Chunker 纯内存计算，不调用 Embedding / Milvus / MySQL。
    5) **不执行业务方法**：DI 仅 return 对象，不 split()。

    Returns:
        Chunker: 待调用的切分器实例（调用方需自行调用 .split(text)）。
    """
    settings = get_settings()
    return RecursiveCharacterChunker(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
    )


@lru_cache(maxsize=1)
def get_document_upload_service() -> DocumentUploadService:
    """
    返回 DocumentUploadService 单例（Phase 2.10 Step 3 新增）。

    依赖链：
        get_document_repository()      → DocumentRepository (Protocol)
        get_file_storage()             → FileStorage (Protocol)
        get_text_parser()              → DocumentParser (Protocol)
        get_chunker()                  → Chunker (Protocol)
        get_document_ingest_service()  → DocumentIngestService
        Settings.max_page_content_bytes
            ↓
        DocumentUploadService(...)

    只负责依赖装配：
    - 获取已缓存的下游单例（DocumentRepository / FileStorage / Parser / Chunker /
      DocumentIngestService）；
    - 从 Settings 注入 max_content_bytes（上传大小上限）；
    - 创建 DocumentUploadService 实例（构造函数仅保存依赖引用 + 上限值）。

    不执行（生命周期红线）：
    - upload() 调用（由 API 层按需触发）
    - save / parse / split / ingest_document / create_document / get_document 调用
    - 任何 MySQL / Milvus / 百炼网络请求

    设计要点：
    1) **lru_cache 单例**：DocumentUploadService 无状态（仅持有注入依赖的引用
       与配置上限），进程内共享一个实例安全且高效，与已有 DI 工厂风格一致。
    2) **依赖复用**：DocumentRepository / FileStorage / Parser / Chunker /
       DocumentIngestService 均为 lru_cache 单例，不会重复创建 Engine / 连接池。
    3) **Protocol 注入**：document_repository / file_storage / parser / chunker
       均为 Protocol 类型，便于单元测试 Mock 与未来切换实现。
    4) **不调用业务方法**：DI 仅 return 对象，不触发上传链路。

    Returns:
        DocumentUploadService: 待调用的上传编排服务实例
        （调用方需自行 await .upload()）。
    """
    document_repository = get_document_repository()
    file_storage = get_file_storage()
    parser = get_text_parser()
    chunker = get_chunker()
    document_ingest_service = get_document_ingest_service()
    settings = get_settings()
    return DocumentUploadService(
        document_repository=document_repository,
        file_storage=file_storage,
        parser=parser,
        chunker=chunker,
        document_ingest_service=document_ingest_service,
        max_content_bytes=settings.max_page_content_bytes,
    )


@lru_cache(maxsize=1)
def get_document_delete_service() -> DocumentDeleteService:
    """
    返回 DocumentDeleteService 单例（Phase 2.11 Step 2 新增）。

    依赖链：
        get_document_repository()  → DocumentRepository (Protocol)
        get_milvus_repository()    → MilvusRepository (Protocol)
        get_file_storage()         → FileStorage (Protocol)
            ↓
        DocumentDeleteService(document_repository, milvus_repository, file_storage)

    只负责依赖装配：
    - 获取已缓存的 DocumentRepository / MilvusRepository / FileStorage 单例；
    - 创建 DocumentDeleteService 实例（构造函数仅保存三个依赖引用）。

    不执行（生命周期红线）：
    - delete_document() 调用（由 API 层按需触发）
    - get_document / update_status / query_page_chunks / delete_chunks /
      file_storage.delete / delete_document 调用
    - 任何 MySQL / Milvus 网络请求

    设计要点：
    1) **lru_cache 单例**：DocumentDeleteService 无状态（仅持有注入依赖的引用），
       进程内共享一个实例安全且高效，与已有 DI 工厂风格一致。
    2) **依赖复用**：DocumentRepository / MilvusRepository / FileStorage 均为
       lru_cache 单例，不会重复创建 Engine / sessionmaker / Milvus 连接池。
    3) **Protocol 注入**：三个依赖均为 Protocol 类型，便于单元测试 Mock。
    4) **不调用业务方法**：DI 仅 return 对象，不触发删除链路。

    Returns:
        DocumentDeleteService: 待调用的删除编排服务实例
        （调用方需自行 await .delete_document()）。
    """
    document_repository = get_document_repository()
    milvus_repository = get_milvus_repository()
    file_storage = get_file_storage()
    return DocumentDeleteService(
        document_repository=document_repository,
        milvus_repository=milvus_repository,
        file_storage=file_storage,
    )


@lru_cache(maxsize=1)
def get_web_clip_service() -> WebClipService:
    """
    返回 WebClipService 单例（Phase 3.1 Step 3 新增）。

    依赖链：
        get_document_repository()      → DocumentRepository (Protocol)
        get_chunker()                  → Chunker (Protocol)
        get_document_ingest_service()  → DocumentIngestService
            ↓
        WebClipService(document_repository, chunker, document_ingest_service)

    只负责依赖装配：
    - 获取已缓存的 DocumentRepository / Chunker / DocumentIngestService 单例；
    - 创建 WebClipService 实例（构造函数仅保存三个依赖引用）。

    不执行（生命周期红线）：
    - clip() 调用（由 API 层按需触发）
    - create_document / update_status / split / ingest_document 调用
    - 任何 MySQL / Milvus / 百炼网络请求

    设计要点：
    1) **lru_cache 单例**：WebClipService 无状态（仅持有注入依赖的引用），
       进程内共享一个实例安全且高效，与已有 DI 工厂风格一致。
    2) **依赖复用**：DocumentRepository / Chunker / DocumentIngestService 均为
       lru_cache 单例，不会重复创建 Engine / sessionmaker / Milvus 连接池。
    3) **Protocol 注入**：document_repository / chunker 均为 Protocol 类型，
       document_ingest_service 为具体服务实例，便于单元测试 Mock。
    4) **不调用业务方法**：DI 仅 return 对象，不触发网页剪藏链路。

    Returns:
        WebClipService: 待调用的网页剪藏编排服务实例
        （调用方需自行 await .clip()）。
    """
    document_repository = get_document_repository()
    chunker = get_chunker()
    document_ingest_service = get_document_ingest_service()
    return WebClipService(
        document_repository=document_repository,
        chunker=chunker,
        document_ingest_service=document_ingest_service,
    )


@lru_cache(maxsize=1)
def get_llm_client() -> LLMClient:
    """
    返回 LLMClient（Protocol）单例（Phase 3.3 Step 3 新增）。

    依赖链：
        Settings (get_default_settings)
            ↓
        BailianLLMClient(settings)
            ↓
        以 LLMClient Protocol 类型返回

    只负责依赖装配：
    - 获取 Settings 单例（default settings，与 get_embedding_client 一致）；
    - 创建 BailianLLMClient 实例（仅保存 settings 引用）。

    不执行（生命周期红线）：
    - OpenAI Client 创建（由 BailianLLMClient._get_client() 在首次 generate() 时惰性创建）；
    - generate() 调用（由 Service 层按需触发）；
    - 任何百炼网络请求。

    设计要点：
    1) **lru_cache 单例**：进程内只构造一次 BailianLLMClient 实例
       （内部 OpenAI client 惰性创建，DI 单例化不会带来连接泄漏）。
    2) **不在 import / 构造时连接百炼**：BailianLLMClient.__init__ 仅保存 Settings
       字段引用，真实 OpenAI client 由 _get_client() 在首次 generate() 时建立。
    3) **返回类型为 Protocol**：上层 Service 依赖 LLMClient 而非 BailianLLMClient，
       便于单元测试 Mock 与未来切换实现。

    Returns:
        LLMClient: 待调用的 LLM 生成客户端实例（调用方需自行调用 .generate()）。
    """
    settings = get_default_settings()
    return BailianLLMClient(settings)


@lru_cache(maxsize=1)
def get_rag_answer_service() -> RagAnswerService:
    """
    返回 RagAnswerService 单例（Phase 3.3 Step 3 新增）。

    依赖链：
        get_rag_service()          → RagService
        get_llm_client()           → LLMClient (Protocol)
        get_document_repository()  → DocumentRepository (Protocol)
            ↓
        RagAnswerService(rag_service, llm_client, document_repository)

    只负责依赖装配：
    - 获取已缓存的 RagService / LLMClient / DocumentRepository 单例；
    - 创建 RagAnswerService 实例（构造函数仅保存三个依赖引用）。

    不执行（生命周期红线）：
    - ask() 调用（由 API 层按需触发）；
    - search / generate / get_document 调用；
    - 任何 MySQL / Milvus / 百炼网络请求。

    设计要点：
    1) **lru_cache 单例**：RagAnswerService 无状态（仅持有注入依赖的引用），
       进程内共享一个实例安全且高效，与已有 DI 工厂风格一致。
    2) **依赖复用**：RagService / LLMClient / DocumentRepository 均为 lru_cache
       单例，不会重复创建 Engine / sessionmaker / OpenAI client / Milvus 连接池。
    3) **Protocol 注入**：llm_client / document_repository 均为 Protocol 类型，
       rag_service 为具体服务实例（通过其自身 DI 工厂装配），便于单元测试 Mock。
    4) **不调用业务方法**：DI 仅 return 对象，不触发 RAG 问答链路。

    Returns:
        RagAnswerService: 待调用的 RAG 问答编排服务实例
        （调用方需自行 await .ask()）。
    """
    rag_service = get_rag_service()
    llm_client = get_llm_client()
    document_repository = get_document_repository()
    return RagAnswerService(
        rag_service=rag_service,
        llm_client=llm_client,
        document_repository=document_repository,
    )


@lru_cache(maxsize=1)
def get_user_repository() -> UserRepository:
    """
    返回 UserRepository（Protocol）实例（Phase 3.4 Step 4 新增）。

    依赖链：
        Engine (core.db.get_engine)
            ↓
        UserRepositoryImpl(engine)
            ↓
        以 UserRepository Protocol 类型返回

    只负责依赖装配：
    - 通过 core.db.get_engine 获取 MySQL Engine 单例（lru_cache 进程内唯一）；
    - 创建 UserRepositoryImpl 实例（构造函数仅保存 engine 引用 + 建 sessionmaker）。

    不执行（生命周期红线）：
    - 任何 SQL 执行（create_user / get_user_by_* / update_token / update_api_key）；
    - MySQL TCP 连接（create_engine 本身不开连接，真实连接在首次 SQL 执行时建立）。

    设计要点：
    1) **返回类型为 Protocol**：上层 Service 用
       `repo: UserRepository = Depends(get_user_repository)` 而非
       UserRepositoryImpl，从而：
         - 单元测试可注入 Mock 实现（同 Protocol，runtime_checkable 校验通过）；
         - 编译期约束：Service 无法访问 Impl 的私有方法/属性，避免越权。
    2) **注入 Engine（而非 Settings）**：Impl 接受 Engine，便于单元测试用
       SQLite in-memory engine 替换（StaticPool），不依赖真实 MySQL。
    3) **lru_cache 单例**：进程内只构造一次 Impl 实例（内部 sessionmaker 共享）。

    Returns:
        UserRepository: Protocol 类型实例（实际为 UserRepositoryImpl，
        但 Service 不感知具体类型）。
    """
    engine = get_engine()
    return UserRepositoryImpl(engine)


@lru_cache(maxsize=1)
def get_user_service() -> UserService:
    """
    返回 UserService 单例（Phase 3.4 Step 4 新增）。

    依赖链：
        get_user_repository()  → UserRepository (Protocol)
        get_settings()         → Settings（app_master_key）
            ↓
        UserService(user_repository, settings)

    只负责依赖装配：
    - 获取已缓存的 UserRepository（Protocol）单例；
    - 获取 Settings 单例（读 app_master_key，AES-256-GCM 主密钥）；
    - 创建 UserService 实例（构造函数仅保存两个依赖引用）。

    不执行（生命周期红线）：
    - register / login / update_api_key / decrypt_api_key 调用；
    - 任何 MySQL 网络请求 / 加密计算。

    设计要点：
    1) **lru_cache 单例**：UserService 无状态（仅持有注入依赖的引用），
       进程内共享一个实例安全且高效，与已有 DI 工厂风格一致。
    2) **依赖复用**：UserRepository 为 lru_cache 单例，不会重复创建
       Engine / sessionmaker。
    3) **Protocol 注入**：user_repository 类型为 UserRepository Protocol，
       UserService 不感知 UserRepositoryImpl 具体类型，便于单元测试 Mock。
    4) **不调用业务方法**：DI 仅 return 对象，不触发认证链路。

    Returns:
        UserService: 待调用的用户认证编排服务实例
        （调用方需自行调用 register / login / decrypt_api_key 等）。
    """
    user_repository = get_user_repository()
    settings = get_settings()
    return UserService(user_repository=user_repository, settings=settings)
