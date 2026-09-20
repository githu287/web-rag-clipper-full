from __future__ import annotations

import asyncio
import logging
import time

from ..core.di import (
    get_ingest_job_repository,
    get_milvus_initializer,
    get_document_upload_service,
    get_plugin_repository,
    get_plugin_service,
    get_redis_ingest_queue,
    get_web_clip_service,
)
from ..core.exceptions import IngestJobOperationError
from ..models.document_api_schema import WebClipRequest
from ..models.ingest_job import IngestJobType
from ..models.plugin import PluginStatus
from ..repositories.mysql.ingest_job_protocol import IngestJobRepository
from ..repositories.mysql.plugin_protocol import PluginRepository
from ..services.plugin_service import PluginService
from ..services.document_upload import DocumentUploadService
from ..services.web_clip import WebClipService
from ..tasks.redis_queue import RedisIngestQueue


logger = logging.getLogger(__name__)


class IngestWorker:
    def __init__(
        self,
        repository: IngestJobRepository | None = None,
        queue: RedisIngestQueue | None = None,
        plugin_repository: PluginRepository | None = None,
        plugin_service: PluginService | None = None,
        web_clip_service: WebClipService | None = None,
        document_upload_service: DocumentUploadService | None = None,
    ) -> None:
        self._repository = repository or get_ingest_job_repository()
        self._queue = queue or get_redis_ingest_queue()
        self._plugin_repository = plugin_repository or get_plugin_repository()
        self._plugin_service = plugin_service or get_plugin_service()
        self._web_clip_service = web_clip_service or get_web_clip_service()
        self._document_upload_service = (
            document_upload_service or get_document_upload_service()
        )

    def run_once(self, timeout_seconds: int = 5) -> bool:
        job_id = self._queue.dequeue(timeout_seconds)
        if job_id is None:
            return False
        job = self._repository.claim_job(job_id)
        if job is None:
            self._acknowledge(job_id)
            return True
        try:
            payload = self._queue.get_payload(job_id)
            if payload is None:
                raise RuntimeError("ingest job payload is missing or expired")
            job_type = payload.get("job_type")
            if job_type not in IngestJobType.ALL or job_type != job.job_type:
                raise RuntimeError("unsupported ingest job type")
            if payload.get("plugin_id") != job.plugin_id:
                raise RuntimeError("ingest job ownership mismatch")

            plugin = self._plugin_repository.get_by_plugin_id(job.plugin_id)
            if plugin is None or plugin.status != PluginStatus.ACTIVE:
                raise RuntimeError("workspace is unavailable")
            api_key = self._plugin_service.decrypt_api_key(plugin)
            if job_type == IngestJobType.WEB_CLIP:
                request = WebClipRequest(
                    url=payload.get("url"),
                    title=payload.get("title"),
                    raw_text=payload.get("raw_text"),
                )
                document = asyncio.run(
                    self._web_clip_service.clip(
                        url=request.url,
                        title=request.title,
                        raw_text=request.raw_text,
                        plugin_id=job.plugin_id,
                        api_key=api_key,
                    )
                )
            else:
                document_id = payload.get("document_id")
                if (
                    not isinstance(document_id, int)
                    or isinstance(document_id, bool)
                    or document_id != job.document_id
                ):
                    raise RuntimeError("ingest job document mismatch")
                document = asyncio.run(
                    self._document_upload_service.process_upload(
                        document_id=document_id,
                        plugin_id=job.plugin_id,
                        api_key=api_key,
                    )
                )
            self._repository.mark_succeeded(job_id, document.id)
            try:
                self._queue.delete_payload(job_id)
            except Exception:  # cleanup failure must not turn a successful ingest into FAILED
                logger.exception("failed to delete completed ingest payload: job_id=%s", job_id)
            self._acknowledge(job_id)
        except Exception as exc:  # worker boundary: persist failure and continue
            logger.exception("ingest job failed: job_id=%s", job_id)
            if (
                job.job_type == IngestJobType.FILE_UPLOAD
                and job.document_id is not None
            ):
                self._document_upload_service.fail_prepared_upload(
                    job.document_id,
                    exc,
                )
            self._repository.mark_failed(job_id, str(exc))
            self._acknowledge(job_id)
        return True

    def _acknowledge(self, job_id: str) -> None:
        try:
            self._queue.acknowledge(job_id)
        except Exception:
            # DB 终态已经是权威结果；ack 失败时保留 processing
            # 记录，重启恢复后 claim 会因非 QUEUED 跳过并再次 ack。
            logger.exception("failed to acknowledge ingest job: job_id=%s", job_id)

    def recover_interrupted(self) -> int:
        job_ids = self._queue.recover_processing()
        for job_id in job_ids:
            self._repository.recover_interrupted(job_id)
        return len(job_ids)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    get_milvus_initializer().initialize()
    worker = IngestWorker()
    recovered = worker.recover_interrupted()
    if recovered:
        logger.warning("recovered %s interrupted ingest jobs", recovered)
    logger.info("ingest worker started")
    try:
        while True:
            try:
                worker.run_once(timeout_seconds=5)
            except IngestJobOperationError:
                # Redis / MySQL 短暂不可用时保持 Worker 存活，避免需要
                # 人工重启才能继续消费。队列的 processing 记录仍会由
                # recover_interrupted() 恢复，不必等待进程重启。
                logger.exception("ingest worker dependency unavailable; retrying")
                time.sleep(2)
                try:
                    recovered = worker.recover_interrupted()
                    if recovered:
                        logger.warning("recovered %s interrupted ingest jobs", recovered)
                except IngestJobOperationError:
                    logger.exception("ingest recovery unavailable; will retry")
    except KeyboardInterrupt:
        logger.info("ingest worker stopped")


if __name__ == "__main__":
    main()
