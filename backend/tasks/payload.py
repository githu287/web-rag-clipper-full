"""Durable job inputs and their original (never extended) web-body deadline."""

from datetime import datetime, timezone
from math import ceil
from typing import Any

from ..models.ingest_job import IngestJob, IngestJobType


def payload_ttl(job: IngestJob) -> int | None:
    deadline = job.payload_expires_at
    if not isinstance(deadline, datetime):
        return None
    remaining = (deadline - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()
    if remaining <= 0:
        raise ValueError("ingest job payload has expired; submit the page again")
    return ceil(remaining)


def durable_payload(job: IngestJob) -> dict[str, Any] | None:
    if job.job_type == IngestJobType.FILE_UPLOAD:
        if not isinstance(job.document_id, int) or isinstance(job.document_id, bool):
            raise ValueError("uploaded document no longer exists")
        return {"job_type": job.job_type, "plugin_id": job.plugin_id,
                "document_id": job.document_id}
    payload_ttl(job)
    if isinstance(job.payload, dict):
        return job.payload
    return None  # Legacy web jobs can still have a body in Redis.
