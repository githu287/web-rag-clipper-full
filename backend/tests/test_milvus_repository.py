from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from backend.repositories.milvus.impl import PyMilvusRepositoryImpl


class MilvusRepositorySearchValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = PyMilvusRepositoryImpl(
            SimpleNamespace(
                milvus_host="localhost",
                milvus_port=19530,
                milvus_collection="page_chunks",
                bailian_embedding_dimension=4,
            )
        )
        self.repository._make_client = Mock()  # noqa: SLF001

    def test_search_rejects_non_positive_limit_before_connecting(self) -> None:
        with self.assertRaisesRegex(ValueError, "limit"):
            self.repository.search([0.0] * 4, limit=0)
        self.repository._make_client.assert_not_called()  # noqa: SLF001

    def test_search_rejects_non_positive_ef_before_connecting(self) -> None:
        with self.assertRaisesRegex(ValueError, "ef"):
            self.repository.search([0.0] * 4, ef=0)
        self.repository._make_client.assert_not_called()  # noqa: SLF001