"""Durable queue with owner filters, workspace locks and fenced worker leases."""
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import sessionmaker

from ...models.plugin import PluginStatus
from ...models.video import VideoJob, VideoModelConfig
from ...core.video_errors import VideoError
from .workspace_lock import lock_workspace

ACTIVE = ("UPLOADING", "QUEUED", "PROCESSING")


class VideoRepository:
    def __init__(self, engine):
        self.sessions = sessionmaker(engine, expire_on_commit=False)

    @staticmethod
    def _active(session, owner):
        workspace = lock_workspace(session, owner)
        if workspace is None or workspace.status != PluginStatus.ACTIVE:
            raise VideoError("空间不可用", 409)
        return workspace

    def config(self, owner):
        with self.sessions() as session:
            return session.get(VideoModelConfig, owner)

    def put_config(self, owner, ciphertext, nonce):
        with self.sessions.begin() as session:
            self._active(session, owner)
            session.merge(VideoModelConfig(plugin_id=owner, ciphertext=ciphertext, nonce=nonce))

    def delete_config(self, owner):
        with self.sessions.begin() as session:
            self._active(session, owner)
            session.execute(delete(VideoModelConfig).where(VideoModelConfig.plugin_id == owner))
            ids = list(session.scalars(select(VideoJob.id).where(VideoJob.plugin_id == owner, VideoJob.status.in_(ACTIVE))))
            session.execute(update(VideoJob).where(VideoJob.plugin_id == owner, VideoJob.status.in_(ACTIVE)).values(
                status="CANCELLED", stage="配置已移除，任务取消", lease=None, lease_until=None))
            return ids

    def create(self, owner, filename, source_url, retention_hours):
        with self.sessions.begin() as session:
            self._active(session, owner)
            count = session.scalar(select(func.count()).select_from(VideoJob).where(VideoJob.plugin_id == owner))
            pending = session.scalar(select(func.count()).select_from(VideoJob).where(VideoJob.plugin_id == owner, VideoJob.status.in_(ACTIVE)))
            if pending >= 3 or count >= 30:
                raise VideoError("视频任务数量已达上限：最多 3 个未完成任务、30 条历史记录，请先删除旧任务", 429)
            job = VideoJob(id=str(uuid4()), plugin_id=owner, filename=filename, source_url=source_url,
                           status="QUEUED" if source_url else "UPLOADING", stage="等待处理" if source_url else "上传中",
                           expires_at=datetime.utcnow() + timedelta(hours=retention_hours))
            session.add(job)
        return job

    def get(self, owner, job_id):
        with self.sessions() as session:
            job = session.scalar(select(VideoJob).where(VideoJob.id == job_id, VideoJob.plugin_id == owner))
            if job is None:
                raise VideoError("视频任务不存在", 404)
            return job

    def list(self, owner):
        with self.sessions() as session:
            return list(session.scalars(select(VideoJob).where(VideoJob.plugin_id == owner).order_by(VideoJob.created_at.desc()).limit(30)))

    def uploaded(self, owner, job_id):
        with self.sessions.begin() as session:
            self._active(session, owner)
            changed = session.execute(update(VideoJob).where(VideoJob.id == job_id, VideoJob.plugin_id == owner, VideoJob.status == "UPLOADING").values(status="QUEUED", stage="等待处理"))
            if changed.rowcount != 1:
                raise VideoError("上传已取消", 409)

    def retry(self, owner, job_id):
        with self.sessions.begin() as session:
            self._active(session, owner)
            job = session.scalar(select(VideoJob).where(VideoJob.id == job_id, VideoJob.plugin_id == owner).with_for_update())
            if job is None:
                raise VideoError("视频任务不存在", 404)
            if job.status != "FAILED" or job.attempts >= 3 or job.expires_at <= datetime.utcnow():
                raise VideoError("仅可重试未过期的失败任务，最多处理 3 次", 409)
            pending = session.scalar(select(func.count()).select_from(VideoJob).where(VideoJob.plugin_id == owner, VideoJob.status.in_(ACTIVE)))
            if pending >= 3:
                raise VideoError("未完成的视频任务已达上限", 429)
            job.status, job.stage, job.error, job.progress = "QUEUED", "等待重试", None, 0

    def cancel(self, owner, job_id):
        with self.sessions.begin() as session:
            changed = session.execute(update(VideoJob).where(VideoJob.id == job_id, VideoJob.plugin_id == owner, VideoJob.status.in_(ACTIVE)).values(status="CANCELLED", stage="已取消", lease=None, lease_until=None))
            if not changed.rowcount:
                # Completed and already cancelled jobs are idempotent; foreign jobs remain 404.
                self.get(owner, job_id)

    def delete(self, owner, job_id):
        with self.sessions.begin() as session:
            changed = session.execute(delete(VideoJob).where(VideoJob.id == job_id, VideoJob.plugin_id == owner))
            if not changed.rowcount:
                raise VideoError("视频任务不存在", 404)

    def claim(self):
        now = datetime.utcnow()
        with self.sessions.begin() as session:
            # Deliberately require manual retry after a crash: never silently bill a second request.
            session.execute(update(VideoJob).where(VideoJob.status == "PROCESSING", VideoJob.lease_until < now).values(status="FAILED", stage="处理已中断", error="视频处理进程中断，请检查结果后手动重试", lease=None, lease_until=None))
            session.execute(update(VideoJob).where(VideoJob.status == "UPLOADING", VideoJob.created_at < now - timedelta(minutes=15)).values(status="FAILED", stage="上传已中断", error="上传中断，请删除任务后重新上传"))
            ids = list(session.scalars(select(VideoJob.id).where(VideoJob.status == "QUEUED", VideoJob.expires_at > now).order_by(VideoJob.created_at).limit(10)))
            for job_id in ids:
                token = str(uuid4())
                changed = session.execute(update(VideoJob).where(VideoJob.id == job_id, VideoJob.status == "QUEUED").values(status="PROCESSING", stage="准备素材", lease=token, lease_until=now + timedelta(minutes=2), attempts=VideoJob.attempts + 1))
                if changed.rowcount:
                    session.flush()
                    return session.get(VideoJob, job_id)
        return None

    def heartbeat(self, job, **values):
        with self.sessions.begin() as session:
            # Lock workspace first, consistently with configuration, retry and deletion.
            workspace = lock_workspace(session, job.plugin_id)
            if workspace is None or workspace.status != PluginStatus.ACTIVE:
                return False
            result = session.execute(update(VideoJob).where(VideoJob.id == job.id, VideoJob.lease == job.lease, VideoJob.status == "PROCESSING", VideoJob.expires_at > datetime.utcnow()).values(lease_until=datetime.utcnow() + timedelta(minutes=2), **values))
            return result.rowcount == 1

    def finish(self, job, *, result=None, error=None, result_days=7):
        values = dict(status="FAILED" if error else "SUCCEEDED", stage="处理失败" if error else "总结完成", error=error, result=result)
        if not error:
            values.update(progress=100, expires_at=datetime.utcnow() + timedelta(days=result_days))
        return self.heartbeat(job, **values)

    def expired(self):
        with self.sessions() as session:
            return list(session.scalars(select(VideoJob).where(VideoJob.expires_at <= datetime.utcnow()).limit(100)))

    def clear_workspace(self, owner):
        with self.sessions.begin() as session:
            workspace = lock_workspace(session, owner)
            if workspace:
                workspace.status = PluginStatus.DELETING
            ids = list(session.scalars(select(VideoJob.id).where(VideoJob.plugin_id == owner)))
            session.execute(delete(VideoJob).where(VideoJob.plugin_id == owner))
            session.execute(delete(VideoModelConfig).where(VideoModelConfig.plugin_id == owner))
            return ids

    def resume_workspace(self, owner):
        with self.sessions.begin() as session:
            workspace = lock_workspace(session, owner)
            if workspace is not None and workspace.status == PluginStatus.DELETING:
                workspace.status = PluginStatus.ACTIVE
