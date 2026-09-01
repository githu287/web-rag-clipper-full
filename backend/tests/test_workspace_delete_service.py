from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

from backend.core.exceptions import PluginDeleteConfirmationError
from backend.services.workspace_delete import WorkspaceDeleteService


class WorkspaceDeleteServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plugins = Mock()
        self.documents = Mock()
        self.document_delete = Mock()
        self.document_delete.delete_document = AsyncMock()
        self.plugins.get_plugin.return_value = SimpleNamespace(plugin_name="My Plugin")
        self.service = WorkspaceDeleteService(
            self.plugins,
            self.documents,
            self.document_delete,
        )

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_deletes_all_documents_before_workspace(self) -> None:
        self.documents.list_documents.side_effect = [
            [SimpleNamespace(id=1), SimpleNamespace(id=2)],
            [],
        ]

        self.run_async(
            self.service.delete_workspace(
                "plugin-a", confirm=True, plugin_name="My Plugin"
            )
        )

        self.document_delete.delete_document.assert_has_awaits(
            [call(1, "plugin-a"), call(2, "plugin-a")]
        )
        self.plugins.delete_workspace.assert_called_once_with(
            "plugin-a", True, "My Plugin"
        )

    def test_confirmation_failure_has_no_side_effects(self) -> None:
        with self.assertRaises(PluginDeleteConfirmationError):
            self.run_async(
                self.service.delete_workspace(
                    "plugin-a", confirm=True, plugin_name="Wrong"
                )
            )
        self.documents.list_documents.assert_not_called()
        self.document_delete.delete_document.assert_not_awaited()
        self.plugins.delete_workspace.assert_not_called()

    def test_document_delete_failure_keeps_workspace(self) -> None:
        self.documents.list_documents.return_value = [SimpleNamespace(id=1)]
        self.document_delete.delete_document.side_effect = RuntimeError("milvus down")
        with self.assertRaises(RuntimeError):
            self.run_async(
                self.service.delete_workspace(
                    "plugin-a", confirm=True, plugin_name="My Plugin"
                )
            )
        self.plugins.delete_workspace.assert_not_called()


if __name__ == "__main__":
    unittest.main()
