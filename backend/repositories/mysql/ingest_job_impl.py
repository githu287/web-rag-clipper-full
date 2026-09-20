from __future__ import annotations

from uuid import uuid4

from sqlalchemy import Engine, func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from ...core.exceptions import (
    IngestJobConflictError,
    IngestJobNotFoundError,
    IngestJobOperationError,
)
from ...models.ingest_job import IngestJob, IngestJobStatus, IngestJobType
from .ingest_job_protocol import IngestJobRepository


_MAX_ERROR_LENGTH = 2048


class IngestJobRepositoryImpl(IngestJobRepository):
    def __init__(self, engine: Engine) -> None:
        self._session_factory: sessionmaker[Session] = sessionmaker(
            bind=engine,
            expire_on_commit=False,
        )

    def create_job(
        self,
        plugin_id: str,
        job_type: str,
        document_id: int | None = None,
    ) -> IngestJob:
        if job_type not in IngestJobType.ALL:
            raise ValueError(f"unsupported ingest job type: {job_type}")
        try:
            with self._session_factory() as session:
                job = IngestJob(
                    id=str(uuid4()),
                    plugin_id=plugin_id,
                    job_type=job_type,
                    document_id=document_id,
                )
                session.add(job)
                session.commit()
                session.refresh(job)
                return job
        except (OperationalError, IntegrityError, DBAPIError) as exc:
            raise IngestJobOperationError("create ingest job failed") from exc

    def get_job(self, job_id: str, plugin_id: str) -> IngestJob:
        try:
            with self._session_factory() as session:
                job = session.scalar(
                    select(IngestJob).where(
                        IngestJob.id == job_id,
                        IngestJob.plugin_id == plugin_id,
                    )
                )
                if job is None:
                    raise IngestJobNotFoundError("ingest job not found")
                session.expunge(job)
                return job
        except IngestJobNotFoundError:
            raise
        except (OperationalError, DBAPIError) as exc:
            raise IngestJobOperationError("get ingest job failed") from exc

    def claim_job(self, job_id: str) -> IngestJob | None:
        """Atomically transition QUEUED to RUNNING; duplicate deliveries return None."""
        try:
            with self._session_factory() as session:
                result = session.execute(
                    update(IngestJob)
                    .where(
                        IngestJob.id == job_id,
                        IngestJob.status == IngestJobStatus.QUEUED,
                    )
                    .values(
                        status=IngestJobStatus.RUNNING,
                        stage="INGESTING",
                        progress=10,
                        attempt_count=IngestJob.attempt_count + 1,
                        started_at=func.now(),
                        finished_at=None,
                        error_message=None,
                        updated_at=func.now(),
                    )
                )
                if result.rowcount != 1:
                    session.rollback()
                    return None
                session.commit()
                job = session.get(IngestJob, job_id)
                if job is None:
                    return None
                session.expunge(job)
                return job
        except (OperationalError, DBAPIError) as exc:
            raise IngestJobOperationError("claim ingest job failed") from exc

    def mark_succeeded(self, job_id: str, document_id: int) -> IngestJob:
        return self._finish_job(
            job_id,
            status=IngestJobStatus.SUCCEEDED,
            stage="COMPLETED",
            progress=100,
            document_id=document_id,
            error_message=None,
        )

    def recover_interrupted(self, job_id: str) -> bool:
        """Reset a worker-crashed RUNNING job so a recovered queue item can run."""
        try:
            with self._session_factory() as session:
                result = session.execute(
                    update(IngestJob)
                    .where(
                        IngestJob.id == job_id,
                        IngestJob.status == IngestJobStatus.RUNNING,
                    )
                    .values(
                        status=IngestJobStatus.QUEUED,
                        stage="QUEUED",
                        progress=0,
                        error_message="worker interrupted; job recovered",
                        started_at=None,
                        finished_at=None,
                        updated_at=func.now(),
                    )
                )
                session.commit()
                return result.rowcount == 1
        except (OperationalError, DBAPIError) as exc:
            raise IngestJobOperationError("recover ingest job failed") from exc

    def mark_failed(self, job_id: str, error_message: str) -> IngestJob:
        return self._finish_job(
            job_id,
            status=IngestJobStatus.FAILED,
            stage="FAILED",
            progress=100,
            document_id=None,
            error_message=(error_message[:_MAX_ERROR_LENGTH] or "ingest failed"),
        )

    def _finish_job(
        self,
        job_id: str,
        *,
        status: str,
        stage: str,
        progress: int,
        document_id: int | None,
        error_message: str | None,
    ) -> IngestJob:
        try:
            with self._session_factory() as session:
                job = session.get(IngestJob, job_id)
                if job is None:
                    raise IngestJobNotFoundError("ingest job not found")
                job.status = status
                job.stage = stage
                job.progress = progress
                if document_id is not None:
                    job.document_id = document_id
                job.error_message = error_message
                job.finished_at = func.now()
                session.commit()
                session.refresh(job)
                return job
        except IngestJobNotFoundError:
            raise
        except (OperationalError, DBAPIError) as exc:
            raise IngestJobOperationError("finish ingest job failed") from exc

    def requeue(self, job_id: str, plugin_id: str) -> IngestJob:
        try:
            with self._session_factory() as session:
                job = session.scalar(
                    select(IngestJob).where(
                        IngestJob.id == job_id,
                        IngestJob.plugin_id == plugin_id,
                    )
                )
                if job is None:
                    raise IngestJobNotFoundError("ingest job not found")
                if job.status != IngestJobStatus.FAILED:
                    raise IngestJobConflictError("only FAILED jobs can be retried")
                job.status = IngestJobStatus.QUEUED
                job.stage = "QUEUED"
                job.progress = 0
                job.error_message = None
                job.document_id = None
                job.started_at = None
                job.finished_at = None
                session.commit()
                session.refresh(job)
                return job
        except (IngestJobNotFoundError, IngestJobConflictError):
            raise
        except (OperationalError, DBAPIError) as exc:
            raise IngestJobOperationError("requeue ingest job failed") from exc
