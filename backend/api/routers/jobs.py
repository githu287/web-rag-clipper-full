from __future__ import annotations

from fastapi import APIRouter, Depends, status

from ...core.di import get_ingest_job_service
from ...models import PluginWorkspace
from ...models.ingest_job import IngestJob
from ...models.ingest_job_api_schema import IngestJobResponse
from ...services.ingest_job import IngestJobService
from ..deps import get_current_plugin


router = APIRouter(prefix="/jobs", tags=["jobs"])


def build_job_response(job: IngestJob) -> IngestJobResponse:
    return IngestJobResponse(
        id=job.id,
        job_type=job.job_type,
        status=job.status,
        stage=job.stage,
        progress=job.progress,
        attempt_count=job.attempt_count,
        document_id=job.document_id,
        error_message=job.error_message,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


@router.get("/{job_id}", response_model=IngestJobResponse)
def get_job(
    job_id: str,
    current_plugin: PluginWorkspace = Depends(get_current_plugin),
    service: IngestJobService = Depends(get_ingest_job_service),
) -> IngestJobResponse:
    return build_job_response(service.get_job(job_id, current_plugin.plugin_id))


@router.post(
    "/{job_id}/retry",
    response_model=IngestJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_job(
    job_id: str,
    current_plugin: PluginWorkspace = Depends(get_current_plugin),
    service: IngestJobService = Depends(get_ingest_job_service),
) -> IngestJobResponse:
    return build_job_response(service.retry_job(job_id, current_plugin.plugin_id))
