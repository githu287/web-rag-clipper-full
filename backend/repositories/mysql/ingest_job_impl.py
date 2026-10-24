from __future__ import annotations

from uuid import uuid4
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Engine, case, func, or_, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import Session, defer, sessionmaker

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
        *,
        payload: dict[str, Any] | None = None,
        payload_ttl_seconds: int | None = None,
    ) -> IngestJob:
        if job_type not in IngestJobType.ALL:
            raise ValueError(f"unsupported ingest job type: {job_type}")
        if payload is not None and payload_ttl_seconds is None:
            payload_ttl_seconds = 604800  # Compatibility for pre-deadline callers.
        if payload is not None and payload_ttl_seconds < 1:
            raise ValueError("durable payload requires a positive TTL")
        try:
            with self._session_factory() as session:
                job = IngestJob(
                    id=str(uuid4()),
                    plugin_id=plugin_id,
                    job_type=job_type,
                    document_id=document_id,
                    payload=payload,
                    payload_expires_at=(
                        datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=payload_ttl_seconds)
                        if payload is not None else None
                    ),
                )
                session.add(job)
                session.commit()
                session.refresh(job)
                return job
        except (OperationalError, IntegrityError, DBAPIError) as exc:
            raise IngestJobOperationError("create ingest job failed") from exc

    def get_job(self, job_id: str, plugin_id: str, *, include_payload: bool = True) -> IngestJob:
        try:
            with self._session_factory() as session:
                statement = select(IngestJob).where(
                    IngestJob.id == job_id, IngestJob.plugin_id == plugin_id,
                )
                if not include_payload:
                    statement = statement.options(defer(IngestJob.payload))
                job = session.scalar(statement)
                if job is None:
                    raise IngestJobNotFoundError("ingest job not found")
                session.expunge(job)
                return job
        except IngestJobNotFoundError:
            raise
        except (OperationalError, DBAPIError) as exc:
            raise IngestJobOperationError("get ingest job failed") from exc

    def list_queued_jobs(self, limit: int = 100) -> list[IngestJob]:
        try:
            with self._session_factory() as session:
                return list(session.scalars(select(IngestJob).where(
                    IngestJob.status == IngestJobStatus.QUEUED,
                    or_(IngestJob.payload.is_not(None), IngestJob.job_type == IngestJobType.FILE_UPLOAD),
                ).order_by(IngestJob.created_at, IngestJob.id).limit(limit)))
        except DBAPIError as exc:
            raise IngestJobOperationError("list queued ingest jobs failed") from exc

    def list_queued(self, limit: int = 20) -> list[IngestJob]:
        if limit < 1:
            raise ValueError("limit must be positive")
        try:
            with self._session_factory() as session:
                return list(session.scalars(select(IngestJob).where(
                    IngestJob.status == IngestJobStatus.QUEUED,
                ).order_by(IngestJob.created_at, IngestJob.id).limit(limit)))
        except (OperationalError, DBAPIError) as exc:
            raise IngestJobOperationError("list queued ingest jobs failed") from exc

    def list_running_ids(self, limit: int = 100) -> list[str]:
        if limit < 1:
            raise ValueError("limit must be positive")
        try:
            with self._session_factory() as session:
                return list(session.scalars(select(IngestJob.id).where(
                    IngestJob.status == IngestJobStatus.RUNNING,
                ).order_by(IngestJob.created_at, IngestJob.id).limit(limit)))
        except (OperationalError, DBAPIError) as exc:
            raise IngestJobOperationError("list running ingest jobs failed") from exc

    def purge_expired_payloads(self, limit: int = 20) -> int:
        """Bounded cleanup also covers failed jobs that are never retried."""
        if limit < 1:
            raise ValueError("limit must be positive")
        try:
            with self._session_factory() as session:
                ids = list(session.scalars(select(IngestJob.id).where(
                    IngestJob.payload_expires_at <= datetime.now(timezone.utc).replace(tzinfo=None),
                    IngestJob.payload.is_not(None),
                ).order_by(IngestJob.payload_expires_at, IngestJob.id).limit(limit).with_for_update()))
                if not ids:
                    return 0
                # Keep the expired deadline as evidence; NULL payload alone is
                # also used by legacy jobs that can still have a Redis body.
                session.execute(update(IngestJob).where(IngestJob.id.in_(ids)).values(payload=None))
                session.commit()
                return len(ids)
        except (OperationalError, DBAPIError) as exc:
            raise IngestJobOperationError("purge ingest payloads failed") from exc

    def get_document_job(self, document_id: int, plugin_id: str) -> IngestJob:
        """Prefer active work, otherwise the latest job; scope BOTH rows to owner.

        Do not select only FAILED jobs: an older failure must not be retried while
        another job is queued/running or has already succeeded.
        """
        try:
            with self._session_factory() as session:
                job = session.scalar(select(IngestJob).join(
                    Document, Document.id == IngestJob.document_id,
                ).where(
                    IngestJob.document_id == document_id,
                    IngestJob.plugin_id == plugin_id,
                    Document.plugin_id == plugin_id,
                ).order_by(
                    case((IngestJob.status.in_([IngestJobStatus.QUEUED, IngestJobStatus.RUNNING]), 0), else_=1),
                    IngestJob.created_at.desc(), IngestJob.updated_at.desc(), IngestJob.id.desc(),
                ).limit(1))
                if job is None:
                    raise IngestJobNotFoundError("document ingest job not found")
                session.expunge(job)
                return job
        except DBAPIError as exc:
            raise IngestJobOperationError("get document ingest job failed") from exc

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

    def recover_interrupted(self, job_id: str, max_attempts: int | None = None) -> bool:
        """Repair a RUNNING job atomically; return True only when requeued.

        Persisted successful evidence wins over the attempt limit: the work is
        already done, so only its job metadata needs completing. Otherwise the
        attempt limit and document release commit together before Redis moves.
        """
        if max_attempts is not None and max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        try:
            with self._session_factory() as session:
                job = session.scalar(select(IngestJob).where(
                    IngestJob.id == job_id,
                ).with_for_update())
                if job is None or job.status != IngestJobStatus.RUNNING:
                    return False
                document = None
                if job.stage == "PROCESSING_DOCUMENT" and job.document_id is not None:
                    document = session.scalar(select(Document).where(
                        Document.id == job.document_id, Document.plugin_id == job.plugin_id,
                    ).with_for_update())
                if document is not None and document.status == DocumentStatus.SUCCESS:
                    job.status = IngestJobStatus.SUCCEEDED
                    job.stage = "COMPLETED"
                    job.progress = 100
                    job.error_message = None
                    job.finished_at = func.now()
                    job.payload = None
                    job.payload_expires_at = None
                else:
                    exhausted = max_attempts is not None and job.attempt_count >= max_attempts
                    message = (
                        "worker interrupted; retry limit reached" if exhausted
                        else "worker interrupted; job recovered"
                    )
                    self._release_document(session, job, message)
                    job.status = IngestJobStatus.FAILED if exhausted else IngestJobStatus.QUEUED
                    job.stage = "FAILED" if exhausted else "QUEUED"
                    job.progress = 100 if exhausted else 0
                    job.error_message = message
                    job.finished_at = func.now() if exhausted else None
                    if not exhausted:
                        job.started_at = None
                job.updated_at = func.now()
                requeued = job.status == IngestJobStatus.QUEUED
                session.commit()
                return requeued
        except (OperationalError, DBAPIError) as exc:
            raise IngestJobOperationError("recover ingest job failed") from exc

    def mark_failed(
        self, job_id: str, error_message: str, *,
        expected_attempt_count: int | None = None,
    ) -> IngestJob:
        """With an attempt count, compensate only an unclaimed submission."""
        return self._finish_job(
            job_id,
            status=IngestJobStatus.FAILED,
            stage="FAILED",
            progress=100,
            document_id=None,
            error_message=(error_message[:_MAX_ERROR_LENGTH] or "ingest failed"),
            expected_attempt_count=expected_attempt_count,
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
        expected_attempt_count: int | None = None,
    ) -> IngestJob:
        try:
            with self._session_factory() as session:
                job = session.scalar(select(IngestJob).where(
                    IngestJob.id == job_id,
                ).with_for_update())
                if expected_attempt_count is not None:
                    if job is None:
                        raise IngestJobNotFoundError("ingest job not found")
                    if (
                        job.status != IngestJobStatus.QUEUED
                        or job.attempt_count != expected_attempt_count
                    ):
                        raise IngestJobConflictError("submission already advanced")
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
                    values["payload_expires_at"] = None
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

    def requeue(self, job_id: str, plugin_id: str, *, max_attempts: int | None = None) -> IngestJob:
        if max_attempts is not None and max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        try:
            with self._session_factory() as session:
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                result = session.execute(
                    update(IngestJob)
                    .where(
                        IngestJob.id == job_id,
                        IngestJob.plugin_id == plugin_id,
                        IngestJob.status == IngestJobStatus.FAILED,
                        or_(IngestJob.payload_expires_at.is_(None), IngestJob.payload_expires_at > now),
                        or_(IngestJob.job_type != IngestJobType.FILE_UPLOAD, IngestJob.document_id.is_not(None)),
                        IngestJob.attempt_count < max_attempts if max_attempts is not None else True,
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
                    if max_attempts is not None and job.attempt_count >= max_attempts:
                        raise IngestJobConflictError("ingest job retry limit reached")
                    if job.payload_expires_at is not None and job.payload_expires_at <= now:
                        raise IngestJobConflictError("ingest job payload has expired; submit the page again")
                    if job.job_type == IngestJobType.FILE_UPLOAD and job.document_id is None:
                        raise IngestJobConflictError("uploaded document no longer exists")
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
