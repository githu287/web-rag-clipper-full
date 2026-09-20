from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class IngestJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    job_type: str
    status: str
    stage: str
    progress: int = Field(ge=0, le=100)
    attempt_count: int = Field(ge=0)
    document_id: int | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
