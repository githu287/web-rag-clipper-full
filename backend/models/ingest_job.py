"""Persistent metadata for asynchronous ingest jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class IngestJobStatus:
    QUEUED: Final[str] = "QUEUED"
    RUNNING: Final[str] = "RUNNING"
    SUCCEEDED: Final[str] = "SUCCEEDED"
    FAILED: Final[str] = "FAILED"
    ALL: Final[frozenset[str]] = frozenset({QUEUED, RUNNING, SUCCEEDED, FAILED})


class IngestJobType:
    WEB_CLIP: Final[str] = "WEB_CLIP"
    FILE_UPLOAD: Final[str] = "FILE_UPLOAD"
    ALL: Final[frozenset[str]] = frozenset({WEB_CLIP, FILE_UPLOAD})


class IngestJob(Base):
    __tablename__ = "ingest_jobs"
    __table_args__ = (
        Index("ix_ingest_jobs_plugin_status", "plugin_id", "status"),
        Index("ix_ingest_jobs_document_id", "document_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plugin_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "plugin_workspaces.plugin_id",
            name="fk_ingest_jobs_plugin_id_plugin_workspaces",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    document_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "documents.id",
            name="fk_ingest_jobs_document_id_documents",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=IngestJobStatus.QUEUED,
        server_default=text("'QUEUED'"),
    )
    stage: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="QUEUED",
        server_default=text("'QUEUED'"),
    )
    progress: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
