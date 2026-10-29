"""Independent cloud-video credentials and durable summary jobs (no raw media in SQL)."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.mysql import LONGTEXT

from .base import Base


class VideoModelConfig(Base):
    __tablename__ = "video_model_configs"
    plugin_id: Mapped[str] = mapped_column(String(64), ForeignKey("plugin_workspaces.plugin_id", ondelete="CASCADE"), primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    nonce: Mapped[str] = mapped_column(String(32))


class VideoJob(Base):
    __tablename__ = "video_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plugin_id: Mapped[str] = mapped_column(String(64), ForeignKey("plugin_workspaces.plugin_id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(180))
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    stage: Mapped[str] = mapped_column(String(80), default="等待处理")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    result: Mapped[str | None] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"), nullable=True)
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
