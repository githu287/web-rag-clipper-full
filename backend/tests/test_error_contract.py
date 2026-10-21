"""
异常 → HTTP 契约测试（优化第二轮 A4：把状态码与响应体形状整份钉住）。

为什么单独一张表：main.py 的 20 个 exception_handler 里，DocumentUploadError /
DocumentStorageError / DocumentParserError / PluginError / PluginRepositoryError
五个族靠 isinstance 分支决定状态码；既有测试是约 60 处分散的
`assertEqual(response.status_code, ...)`，新增子类或调整分支时没有任何一处会整体
失败。本文件把「异常类 → 状态码 → 响应体键 → 响应体值 → detail 是否回显原始消息」
收敛成一张表逐项实跑（探针路由真的走 Starlette 的分派链，不是直接调 handler）。

覆盖用例：
  1. 表内每一行：探针路由抛出该异常 → 断言状态码、响应体键集合恰好是
     {detail, type|code}、该键的值、以及 detail 的消息回显策略。
  2. 守门 A：每个注册在 app 上的业务异常 handler 都必须在表里有行
     （新增 handler 却忘了登记契约 → 红）。
  3. 守门 B：表里每个异常类都必须真的会被某个业务 handler 接住
     （登记了没人处理的异常 → 红，否则会静默落到 Starlette 默认 500）。
  4. 双响应体形状规则：只有 IngestJob 族用 code，其余一律用 type
     （ARCHITECTURE.md「错误响应」小节记录的同一规则）。

技术栈：FastAPI TestClient + patch("backend.main.get_milvus_initializer") 阻断
lifespan 的真实 Milvus 连接，探针路由在 create_app() 之后追加；
不连 MySQL / Milvus / 百炼 / Redis。
"""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from backend.clients.embedding import EmbeddingClientError, EmbeddingConfigError
from backend.clients.llm import LLMClientError, LLMClientRequestError
from backend.core.exceptions import (
    ApiKeyNotConfiguredError,
    ApiKeyValidationError,
    DocumentChunkingConfigError,
    DocumentChunkingError,
    DocumentFileEmptyError,
    DocumentFileTooLargeError,
    DocumentNotSuccessError,
    DocumentNotFoundError,
    DocumentOperationError,
    DocumentParserError,
    DocumentParserReadError,
    DocumentParserUnsupportedExtensionError,
    DocumentQuotaExceededError,
    DocumentStorageError,
    DocumentStoragePathTraversalError,
    DocumentUnsupportedExtensionError,
    DocumentUploadError,
    IngestJobConflictError,
    IngestJobNotFoundError,
    IngestJobOperationError,
    MilvusConnectionError,
    MilvusRepositoryError,
    MilvusSchemaMismatchError,
    PluginCredentialsMissingError,
    PluginDeleteConfirmationError,
    PluginDisabledError,
    PluginError,
    PluginNameTakenError,
    PluginNameValidationError,
    PluginNotFoundError,
    PluginOperationError,
    PluginRepositoryError,
    PluginSecretMismatchError,
    SecurityConfigurationError,
    SecurityDecryptionError,
)
from backend.main import create_app

# 探针异常的原始消息：用于断言 detail 是否原样回显（面向用户的中文文案会走回显，
# 少数 handler 刻意用固定串替换，见 _FIXED_DETAILS）。
_PROBE_MESSAGE = "契约探针错误消息"

# 契约表：异常类 → (状态码, 响应体判别键, 该键的期望值)。
# type 行的期望值恒等于异常类名（handler 统一写 type(exc).__name__），显式写出来
# 是为了让「重命名异常类」变成一次需要同时改文档的契约变更，而不是静默漂移。
_ERROR_CONTRACT: list[tuple[type, int, str, str]] = [
    # ---------------------------------------- IngestJob 族（唯一使用 code 的族）
    (IngestJobNotFoundError, 404, "code", "INGEST_JOB_NOT_FOUND"),
    (IngestJobConflictError, 409, "code", "INGEST_JOB_CONFLICT"),
    (IngestJobOperationError, 503, "code", "INGEST_QUEUE_UNAVAILABLE"),
    # -------------------------------------------------------------- Milvus 族 → 503
    (MilvusRepositoryError, 503, "type", "MilvusRepositoryError"),
    (MilvusConnectionError, 503, "type", "MilvusConnectionError"),
    (MilvusSchemaMismatchError, 503, "type", "MilvusSchemaMismatchError"),
    # ------------------------------------------------------------ 百炼客户端 → 502
    (EmbeddingClientError, 502, "type", "EmbeddingClientError"),
    (EmbeddingConfigError, 502, "type", "EmbeddingConfigError"),
    (LLMClientError, 502, "type", "LLMClientError"),
    (LLMClientRequestError, 502, "type", "LLMClientRequestError"),
    # ------------------------------------------------------------- Document 数据层
    (DocumentNotSuccessError, 409, "type", "DocumentNotSuccessError"),
    (DocumentNotFoundError, 404, "type", "DocumentNotFoundError"),
    (DocumentOperationError, 503, "type", "DocumentOperationError"),
    # --------------------------------------------------- DocumentUploadError 分支表
    (DocumentUploadError, 400, "type", "DocumentUploadError"),
    (DocumentFileTooLargeError, 413, "type", "DocumentFileTooLargeError"),
    (DocumentUnsupportedExtensionError, 415, "type", "DocumentUnsupportedExtensionError"),
    (DocumentFileEmptyError, 400, "type", "DocumentFileEmptyError"),
    (DocumentQuotaExceededError, 429, "type", "DocumentQuotaExceededError"),
    # -------------------------------------------------- DocumentStorageError 分支表
    (DocumentStorageError, 500, "type", "DocumentStorageError"),
    (DocumentStoragePathTraversalError, 400, "type", "DocumentStoragePathTraversalError"),
    # --------------------------------------------------- DocumentParserError 分支表
    (DocumentParserError, 500, "type", "DocumentParserError"),
    (
        DocumentParserUnsupportedExtensionError,
        400,
        "type",
        "DocumentParserUnsupportedExtensionError",
    ),
    (DocumentParserReadError, 500, "type", "DocumentParserReadError"),
    # ------------------------------------------------------------- Document 切分 → 500
    (DocumentChunkingError, 500, "type", "DocumentChunkingError"),
    (DocumentChunkingConfigError, 500, "type", "DocumentChunkingConfigError"),
    # ------------------------------------------------------------------- API Key 配置
    (ApiKeyValidationError, 400, "type", "ApiKeyValidationError"),
    (ApiKeyNotConfiguredError, 409, "type", "ApiKeyNotConfiguredError"),
    # ----------------------------------------------------------------------- 安全层 → 500
    (SecurityConfigurationError, 500, "type", "SecurityConfigurationError"),
    (SecurityDecryptionError, 500, "type", "SecurityDecryptionError"),
    # ------------------------------------------------------- PluginError 业务族分支表
    (PluginError, 401, "type", "PluginError"),
    (PluginCredentialsMissingError, 401, "type", "PluginCredentialsMissingError"),
    (PluginSecretMismatchError, 401, "type", "PluginSecretMismatchError"),
    (PluginDisabledError, 403, "type", "PluginDisabledError"),
    (PluginNameTakenError, 409, "type", "PluginNameTakenError"),
    (PluginNameValidationError, 422, "type", "PluginNameValidationError"),
    (PluginDeleteConfirmationError, 400, "type", "PluginDeleteConfirmationError"),
    # ------------------------------------------------- PluginRepositoryError 分支表
    (PluginRepositoryError, 503, "type", "PluginRepositoryError"),
    (PluginNotFoundError, 401, "type", "PluginNotFoundError"),
    (PluginOperationError, 503, "type", "PluginOperationError"),
]

# 刻意不回显原始异常的 detail（main.py 里唯一一处把消息换成固定串，避免把队列内部
# 错误透给客户端）。命中此表的行断言 detail **等于**固定串，其余行断言**包含**探针消息。
_FIXED_DETAILS: dict[type, str] = {
    IngestJobOperationError: "ingest queue unavailable",
}

_ROUTE_PATH = "/__error-contract/{index}"


class ErrorContractTest(unittest.TestCase):
    """整份异常 → HTTP 契约表实跑 + 双向守门。"""

    @classmethod
    def setUpClass(cls) -> None:
        """构建一次 app（探针路由与表同生命期），隔离 lifespan 的真实 Milvus 连接。"""
        cls._milvus_init_patcher = patch("backend.main.get_milvus_initializer")
        mock_initializer = Mock()
        mock_initializer.initialize.return_value = None
        cls._milvus_init_patcher.start().return_value = mock_initializer

        cls.app = create_app()

        def probe(index: int) -> None:
            """按索引抛出契约表里登记的异常，走真实异常分派链。"""
            raise _ERROR_CONTRACT[index][0](_PROBE_MESSAGE)

        cls.app.add_api_route(_ROUTE_PATH, probe, methods=["GET"])
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._milvus_init_patcher.stop()

    @classmethod
    def _business_handlers(cls) -> set[type]:
        """app 上注册的业务异常类（过滤掉 Starlette/FastAPI 自带的内建键）。"""
        return {
            key
            for key in cls.app.exception_handlers
            if isinstance(key, type)
            and issubclass(key, Exception)
            and key.__module__.startswith("backend.")
        }

    # ------------------------------------------- 1. 契约表逐行实跑（状态码 + 响应体）
    def test_contract_table(self) -> None:
        """1：每行断言状态码、响应体键集合恰为 {detail, type|code}、判别键值与 detail 策略。"""
        self.assertTrue(_ERROR_CONTRACT, "契约表不能为空")
        for index, (exc_cls, status_code, body_key, body_value) in enumerate(_ERROR_CONTRACT):
            with self.subTest(exc=exc_cls.__name__, status=status_code):
                response = self.client.get(_ROUTE_PATH.format(index=index))

                self.assertEqual(response.status_code, status_code)
                body = response.json()
                self.assertEqual(set(body.keys()), {"detail", body_key})
                self.assertEqual(body[body_key], body_value)
                fixed_detail = _FIXED_DETAILS.get(exc_cls)
                if fixed_detail is None:
                    self.assertIn(_PROBE_MESSAGE, body["detail"])
                else:
                    self.assertEqual(body["detail"], fixed_detail)

    # ------------------------------------ 2. 守门 A：每个业务 handler 都必须在表里有行
    def test_every_registered_business_handler_is_covered(self) -> None:
        """2：新增 exception_handler 却没登记契约表 → 本用例失败（防清单漂移）。"""
        registered = self._business_handlers()
        self.assertEqual(
            len(registered),
            20,
            "业务异常 handler 数量变化时，请同步本文件与 ARCHITECTURE.md 的状态码表",
        )
        missing = registered - {row[0] for row in _ERROR_CONTRACT}
        self.assertEqual(missing, set())

    # ------------------------------------ 3. 守门 B：表里每行都必须真的会被接住
    def test_every_table_row_is_handled(self) -> None:
        """3：表里登记的异常若没有任何 handler 命中，会静默落到 Starlette 默认 500。"""
        registered = self._business_handlers()
        for exc_cls in {row[0] for row in _ERROR_CONTRACT}:
            with self.subTest(exc=exc_cls.__name__):
                self.assertTrue(
                    any(key in registered for key in exc_cls.__mro__),
                    f"{exc_cls.__name__} 不会被任何业务 handler 接住",
                )

    # --------------------------------------------- 4. 双响应体形状规则（A2/A3 的文档依据）
    def test_only_ingest_job_family_uses_code(self) -> None:
        """4：只有 IngestJob 族返回 code，其余 17 个 handler 一律返回 type。"""
        code_rows = {row[0] for row in _ERROR_CONTRACT if row[2] == "code"}
        self.assertEqual(
            code_rows,
            {IngestJobNotFoundError, IngestJobConflictError, IngestJobOperationError},
        )
        self.assertTrue(all(row[2] in {"code", "type"} for row in _ERROR_CONTRACT))


if __name__ == "__main__":
    unittest.main()
