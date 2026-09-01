"""Workspace 级删除编排：Document 资源收敛后再删除 Workspace 行。"""

from __future__ import annotations

from ..core.exceptions import PluginDeleteConfirmationError
from ..repositories.mysql import DocumentRepository
from .document_delete import DocumentDeleteService
from .plugin_service import PluginService


class WorkspaceDeleteService:
    """跨 MySQL、Milvus、FileStorage 的 Workspace 删除入口。"""

    def __init__(
        self,
        plugin_service: PluginService,
        document_repository: DocumentRepository,
        document_delete_service: DocumentDeleteService,
    ) -> None:
        self._plugins = plugin_service
        self._documents = document_repository
        self._delete_document = document_delete_service

    async def delete_workspace(
        self,
        plugin_id: str,
        *,
        confirm: bool,
        plugin_name: str,
    ) -> None:
        workspace = self._plugins.get_plugin(plugin_id)
        if not confirm or plugin_name != workspace.plugin_name:
            raise PluginDeleteConfirmationError(
                "workspace deletion requires confirm=true and exact plugin name"
            )

        # 始终读取第一页：每轮删除会使下一批文档前移，避免 offset 跳项。
        while True:
            documents = self._documents.list_documents(
                plugin_id,
                page=1,
                page_size=100,
            )
            if not documents:
                break
            for document in documents:
                await self._delete_document.delete_document(document.id, plugin_id)

        # 复用 PluginService 的二次确认与 Repository 删除，不复制身份规则。
        self._plugins.delete_workspace(plugin_id, confirm, plugin_name)
