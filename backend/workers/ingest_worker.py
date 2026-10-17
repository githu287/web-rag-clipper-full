from __future__ import annotations

import asyncio
import logging
import signal
import threading
import time

from ..core.di import (
    get_ingest_job_repository,
    get_milvus_initializer,
    get_document_upload_service,
    get_plugin_repository,
    get_plugin_service,
    get_redis_ingest_queue,
    get_settings,
    get_web_clip_service,
)
from ..core.exceptions import IngestJobNotFoundError, IngestJobOperationError
from ..models.document_api_schema import WebClipRequest
from ..models.ingest_job import IngestJobStatus, IngestJobType
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
        max_retries: int | None = None,
    ) -> None:
        self._repository = repository or get_ingest_job_repository()
        self._queue = queue or get_redis_ingest_queue()
        self._plugin_repository = plugin_repository or get_plugin_repository()
        self._plugin_service = plugin_service or get_plugin_service()
        self._web_clip_service = web_clip_service or get_web_clip_service()
        self._document_upload_service = (
            document_upload_service or get_document_upload_service()
        )
        self._max_retries = (
            max_retries if max_retries is not None else get_settings().ingest_max_retries
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
                        claim_document=lambda document_id: self._repository.begin_document(job_id, document_id),
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
                        claim_document=lambda document_id: self._repository.begin_document(job_id, document_id),
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
            if self._record_failure(job, exc):
                self._acknowledge(job_id)
        return True

    def _record_failure(self, job, exc: Exception) -> bool:
        """写失败终态；写不进时返回 False，让 processing 保留给恢复流程重跑。"""
        try:
            self._repository.mark_failed(job.id, str(exc))
        except Exception:
            # 终态写入冲突（如已被并发恢复改写）不能拖垮 Worker，也不能 ack，
            # 否则任务停在 RUNNING 且队列里再也不会出现。
            logger.exception("failed to record ingest job failure: job_id=%s", job.id)
            return False
        return True

    def _acknowledge(self, job_id: str) -> None:
        try:
            self._queue.acknowledge(job_id)
        except Exception:
            # DB 终态已经是权威结果；ack 失败时保留 processing
            # 记录，重启恢复后 claim 会因非 QUEUED 跳过并再次 ack。
            logger.exception("failed to acknowledge ingest job: job_id=%s", job_id)

    def recover_interrupted(self) -> int:
        """复位 MySQL 后才搬运队列，避免半途失败导致任务被静默丢弃。"""
        job_ids = self._queue.list_processing()
        recovered = 0
        for job_id in job_ids:
            if self._exhausted_retries(job_id):
                continue
            if self._repository.recover_interrupted(job_id):
                recovered += 1
        # 上面的库侧写入全部就位才搬运；若中途抛 IngestJobOperationError，
        # processing 保持原样，下一轮恢复会重新处理同一批 job。
        self._queue.recover_processing()
        return recovered

    def _exhausted_retries(self, job_id: str) -> bool:
        """崩溃恢复不经过 retry_job，这里补上同一个重试上限，避免毒任务无限重入队列。"""
        try:
            payload = self._queue.get_payload(job_id)
        except IngestJobOperationError:
            return False
        if not isinstance(payload, dict):
            # payload 已过期或不可读时无法判定归属，维持原有的直接恢复
            return False
        plugin_id = payload.get("plugin_id")
        if not isinstance(plugin_id, str):
            return False
        try:
            job = self._repository.get_job(job_id, plugin_id)
        except (IngestJobNotFoundError, IngestJobOperationError):
            return False
        if job.status != IngestJobStatus.RUNNING or job.attempt_count < self._max_retries:
            return False
        give_up = "worker interrupted; retry limit reached"
        logger.error(
            "ingest job exceeded retry limit during recovery: job_id=%s attempts=%s",
            job_id,
            job.attempt_count,
        )
        self._record_failure(job, RuntimeError(give_up))
        return True


def _install_sigterm_handler(stop: threading.Event) -> None:
    """SIGTERM 只置位停止标志，不在任务中途终止进程。

    正在入库的任务被杀掉会让文档停在「处理中」，只能等下次启动的
    recover_interrupted() 兜底；置位后主循环会在当前任务收尾时退出。
    """

    def handle(signum, _frame) -> None:
        logger.info("ingest worker received signal %s; finishing current job", signum)
        stop.set()

    signal.signal(signal.SIGTERM, handle)


def main(stop: threading.Event) -> None:
    logging.basicConfig(level=logging.INFO)
    get_milvus_initializer().initialize()
    worker = IngestWorker()
    recovered = worker.recover_interrupted()
    if recovered:
        logger.warning("recovered %s interrupted ingest jobs", recovered)
    logger.info("ingest worker started")
    try:
        while not stop.is_set():
            try:
                worker.run_once(timeout_seconds=5)
            except Exception:
                # 无论是 Redis / MySQL 短暂不可用，还是单个任务引发的意外异常，
                # 都只退避重试：进程退出会丢掉所有在途任务，而队列的 processing
                # 记录仍由 recover_interrupted() 恢复，不必重启。
                logger.exception("ingest worker hit an error; retrying")
                time.sleep(2)
                try:
                    recovered = worker.recover_interrupted()
                    if recovered:
                        logger.warning("recovered %s interrupted ingest jobs", recovered)
                except Exception:
                    logger.exception("ingest recovery unavailable; will retry")
    except KeyboardInterrupt:
        pass
    logger.info("ingest worker stopped")


if __name__ == "__main__":
    stop_event = threading.Event()
    _install_sigterm_handler(stop_event)
    main(stop_event)
