"""Shared database coordination for model changes and document writes."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.exceptions import DocumentOperationError
from ...models.plugin import PluginWorkspace


def lock_workspace(session: Session, plugin_id: str) -> PluginWorkspace | None:
    return session.scalar(select(PluginWorkspace).where(
        PluginWorkspace.plugin_id == plugin_id,
    ).with_for_update())


def check_embedding_space(workspace: PluginWorkspace | None, fingerprint: str | None) -> None:
    if fingerprint is not None and (
        workspace is None or (
            workspace.embedding_config_fingerprint is not None
            and workspace.embedding_config_fingerprint != fingerprint
        )
    ):
        raise DocumentOperationError("Embedding configuration changed; retry with the current model configuration")
