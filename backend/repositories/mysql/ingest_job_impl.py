from __future__ import annotations

from uuid import uuid4

from sqlalchemy import Engine, func, or_, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from ...core.exceptions import (
    IngestJobConflictError,
    IngestJobNotFoundError,
    IngestJobOperationError,
)
from ...models.document import Document, DocumentStatus
from ...models.ingest_job import IngestJob, IngestJobStatus, IngestJobType
from .ingest_job_protocol import IngestJobRepository
from .workspace_lock import check_embedding_space, lock_workspace


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
        payload: dict | None = None,
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
                    payload=payload,
                )
                session.add(job)
                session.commit()
                session.refresh(job)
                return job
        except (OperationalError, IntegrityError, DBAPIError) as exc:
            raise IngestJobOperationError("create ingest job failed") from exc

    def list_queued_jobs(self, limit: int = 100) -> list[IngestJob]:
        try:
            with self._session_factory() as session:
                return list(session.scalars(select(IngestJob).where(
                    IngestJob.status == IngestJobStatus.QUEUED,
                    or_(IngestJob.payload.is_not(None), IngestJob.job_type == IngestJobType.FILE_UPLOAD),
                ).order_by(IngestJob.created_at, IngestJob.id).limit(limit)))
        except DBAPIError as exc:
            raise IngestJobOperationError("list queued ingest jobs failed") from exc

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

    def begin_document(self, job_id: str, document_id: int, *, embedding_fingerprint: str | None = None) -> None:
        """Claim document and bind its RUNNING job in one transaction."""
        try:
            with self._session_factory() as session:
                job = session.scalar(select(IngestJob).where(
                    IngestJob.id == job_id,
                ).with_for_update())
                if job is None or job.status != IngestJobStatus.RUNNING:
                    raise IngestJobConflictError("only RUNNING jobs can claim documents")
                if job.stage == "PROCESSING_DOCUMENT":
                    raise IngestJobConflictError("job already holds a document")
                if embedding_fingerprint is not None:
                    check_embedding_space(lock_workspace(session, job.plugin_id), embedding_fingerprint)
                result = session.execute(update(Document).where(
                    Document.id == document_id, Document.plugin_id == job.plugin_id,
                    Document.status.in_([DocumentStatus.PENDING, DocumentStatus.SUCCESS, DocumentStatus.FAILED]),
                    ~select(IngestJob.id).where(
                        IngestJob.document_id == Document.id,
                        IngestJob.status == IngestJobStatus.RUNNING,
                        IngestJob.stage == "PROCESSING_DOCUMENT",
                    ).exists(),
                ).values(status=DocumentStatus.PROCESSING, error_message=None))
                if result.rowcount != 1:
                    raise IngestJobConflictError("document is unavailable or already processing")
                job.document_id = document_id
                job.stage = "PROCESSING_DOCUMENT"
                session.commit()
        except (OperationalError, DBAPIError) as exc:
            raise IngestJobOperationError("claim job document failed") from exc

    @staticmethod
    def _release_document(session: Session, job: IngestJob, message: str) -> None:
        # Preflight failures (missing credentials/payload) may occur before claim.
        # Only a still-PENDING prepared upload may be failed in that case.
        expected = (
            DocumentStatus.PROCESSING if job.stage == "PROCESSING_DOCUMENT"
            else DocumentStatus.PENDING if job.job_type == IngestJobType.FILE_UPLOAD
            else None
        )
        if expected is not None and job.document_id is not None:
            session.execute(update(Document).where(
                Document.id == job.document_id, Document.plugin_id == job.plugin_id,
                Document.status == expected,
            ).values(status=DocumentStatus.FAILED, error_message=message[:_MAX_ERROR_LENGTH]))

    def recover_interrupted(self, job_id: str) -> bool:
        """Reset a worker-crashed RUNNING job so a recovered queue item can run."""
        try:
            with self._session_factory() as session:
                job = session.scalar(select(IngestJob).where(
                    IngestJob.id == job_id,
                ).with_for_update())
                if job is None or job.status != IngestJobStatus.RUNNING:
                    return False
                self._release_document(session, job, "worker interrupted; retry queued")
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
                job = session.scalar(select(IngestJob).where(
                    IngestJob.id == job_id,
                ).with_for_update())
                if (
                    job is not None
                    and job.status in {IngestJobStatus.RUNNING, IngestJobStatus.QUEUED}
                    and status == IngestJobStatus.FAILED
                ):
                    self._release_document(session, job, error_message or "ingest failed")
                expected_statuses = [IngestJobStatus.RUNNING]
                if status == IngestJobStatus.FAILED:
                    expected_statuses.append(IngestJobStatus.QUEUED)
                values: dict[str, object] = {
                    "status": status,
                    "stage": stage,
                    "progress": progress,
                    "error_message": error_message,
                    "finished_at": func.now(),
                    "updated_at": func.now(),
                }
                if document_id is not None:
                    values["document_id"] = document_id
                if status == IngestJobStatus.SUCCEEDED:
                    values["payload"] = None
                result = session.execute(
                    update(IngestJob)
                    .where(
                        IngestJob.id == job_id,
                        IngestJob.status.in_(expected_statuses),
                    )
                    .values(**values)
                )
                if result.rowcount != 1:
                    job = session.get(IngestJob, job_id)
                    if job is None:
                        raise IngestJobNotFoundError("ingest job not found")
                    raise IngestJobConflictError("job is already terminal")
                session.commit()
                job = session.get(IngestJob, job_id)
                if job is None:
                    raise IngestJobNotFoundError("ingest job not found")
                session.expunge(job)
                return job
        except IngestJobNotFoundError:
            raise
        except (OperationalError, DBAPIError) as exc:
            raise IngestJobOperationError("finish ingest job failed") from exc

    def requeue(self, job_id: str, plugin_id: str) -> IngestJob:
        try:
            with self._session_factory() as session:
                result = session.execute(
                    update(IngestJob)
                    .where(
                        IngestJob.id == job_id,
                        IngestJob.plugin_id == plugin_id,
                        IngestJob.status == IngestJobStatus.FAILED,
                    )
                    .values(
                        status=IngestJobStatus.QUEUED,
                        stage="QUEUED",
                        progress=0,
                        error_message=None,
                        started_at=None,
                        finished_at=None,
                        updated_at=func.now(),
                    )
                )
                if result.rowcount != 1:
                    job = session.scalar(
                        select(IngestJob).where(
                            IngestJob.id == job_id,
                            IngestJob.plugin_id == plugin_id,
                        )
                    )
                    if job is None:
                        raise IngestJobNotFoundError("ingest job not found")
                    raise IngestJobConflictError("only FAILED jobs can be retried")
                session.commit()
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
        except (IngestJobNotFoundError, IngestJobConflictError):
            raise
        except (OperationalError, DBAPIError) as exc:
            raise IngestJobOperationError("requeue ingest job failed") from exc
