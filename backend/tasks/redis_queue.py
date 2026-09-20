from __future__ import annotations

import json
from typing import Any

from redis import Redis
from redis.exceptions import RedisError

from ..core.config import Settings
from ..core.exceptions import IngestJobOperationError


class RedisIngestQueue:
    """Small Redis-backed queue; payloads never contain API keys or secrets."""

    def __init__(self, settings: Settings) -> None:
        self._queue_name = settings.ingest_queue_name
        self._processing_name = f"{settings.ingest_queue_name}:processing"
        self._payload_ttl = settings.ingest_payload_ttl_seconds
        self._client = Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            password=settings.redis_password or None,
            decode_responses=True,
            socket_connect_timeout=3,
            # Worker 的阻塞出队最多等待 5 秒。读取超时必须明显
            # 大于该值，否则空队列会被误判为 Redis 连接故障。
            socket_timeout=10,
        )

    def _payload_key(self, job_id: str) -> str:
        return f"{self._queue_name}:payload:{job_id}"

    def enqueue(self, job_id: str, payload: dict[str, Any]) -> None:
        try:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            with self._client.pipeline(transaction=True) as pipeline:
                pipeline.setex(self._payload_key(job_id), self._payload_ttl, encoded)
                pipeline.lpush(self._queue_name, job_id)
                pipeline.execute()
        except (RedisError, TypeError, ValueError) as exc:
            raise IngestJobOperationError("enqueue ingest job failed") from exc

    def enqueue_existing(self, job_id: str) -> None:
        try:
            if not self._client.exists(self._payload_key(job_id)):
                raise IngestJobOperationError("ingest job payload has expired")
            self._client.lpush(self._queue_name, job_id)
        except IngestJobOperationError:
            raise
        except RedisError as exc:
            raise IngestJobOperationError("requeue ingest job failed") from exc

    def dequeue(self, timeout_seconds: int = 5) -> str | None:
        try:
            return self._client.brpoplpush(
                self._queue_name,
                self._processing_name,
                timeout=timeout_seconds,
            )
        except RedisError as exc:
            raise IngestJobOperationError("dequeue ingest job failed") from exc

    def acknowledge(self, job_id: str) -> None:
        try:
            self._client.lrem(self._processing_name, 1, job_id)
        except RedisError as exc:
            raise IngestJobOperationError("acknowledge ingest job failed") from exc

    def recover_processing(self) -> list[str]:
        """Move unacknowledged jobs back to the pending queue (single worker mode)."""
        recovered: list[str] = []
        try:
            while True:
                job_id = self._client.rpoplpush(
                    self._processing_name,
                    self._queue_name,
                )
                if job_id is None:
                    return recovered
                recovered.append(job_id)
        except RedisError as exc:
            raise IngestJobOperationError("recover ingest queue failed") from exc

    def get_payload(self, job_id: str) -> dict[str, Any] | None:
        try:
            encoded = self._client.get(self._payload_key(job_id))
            if encoded is None:
                return None
            payload = json.loads(encoded)
            return payload if isinstance(payload, dict) else None
        except (RedisError, json.JSONDecodeError) as exc:
            raise IngestJobOperationError("read ingest job payload failed") from exc

    def delete_payload(self, job_id: str) -> None:
        try:
            self._client.delete(self._payload_key(job_id))
        except RedisError as exc:
            raise IngestJobOperationError("delete ingest job payload failed") from exc
