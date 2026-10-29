"""Video jobs and encrypted cloud configuration; previews never implicitly ingest."""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import re
import shutil
import time
from uuid import UUID, uuid4

from ..core.security import decrypt_api_key, encrypt_api_key
from ..core.video_errors import VideoError
from .video_links import normalize_link
from .video_media import ffmpeg_executable

MODELS = ("qwen3-omni-flash", "qwen3.5-omni-flash", "qwen3.5-omni-plus", "qwen3.8-omni-flash")
DEFAULT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".mkv", ".avi")


class VideoSummaryService:
    def __init__(self, repository, settings):
        self.repo = repository
        self.settings = settings
        self.root = Path(settings.upload_dir).resolve() / "video"

    def directory(self, job_id):
        try:
            if str(UUID(job_id)) != job_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise VideoError("视频任务不存在", 404) from None
        return self.root / job_id

    def input_path(self, job_id):
        return self.directory(job_id) / "source.bin"

    @contextmanager
    def working_directory(self):
        # Crash leftovers stay inside the retained private root, never system temp.
        directory = self.directory(str(uuid4()))
        directory.mkdir(parents=True, exist_ok=False)
        try:
            yield directory
        finally:
            shutil.rmtree(directory)

    def remove_media(self, job_id):
        directory = self.directory(job_id)
        if directory.exists():
            shutil.rmtree(directory)

    def config(self, owner, *, secret=False):
        row = self.repo.config(owner)
        if row is None:
            if secret:
                raise VideoError("请先在视频面板配置云端 Qwen-Omni 服务", 409)
            return {"configured": False, "base_url": DEFAULT_URL, "model": MODELS[0], "models": MODELS}
        try:
            data = json.loads(decrypt_api_key(row.ciphertext, row.nonce, self.settings.app_master_key))
        except Exception:
            raise VideoError("视频模型配置无法解密，请重新保存配置", 503) from None
        if secret:
            return data
        return {"configured": True, "base_url": data["base_url"], "model": data["model"], "models": MODELS}

    def put_config(self, owner, payload):
        if not isinstance(payload, dict) or set(payload) - {"api_key", "base_url", "model"}:
            raise VideoError("视频配置字段不正确", 422)
        base_url, model, key = (payload.get(field) for field in ("base_url", "model", "api_key"))
        if not isinstance(base_url, str) or not re.fullmatch(
            r"https://(?:(?:dashscope(?:-intl|-us)?\.aliyuncs\.com)|(?:[a-zA-Z0-9-]+\.(?:cn-beijing|cn-shanghai|ap-southeast-1|us-east-1)\.maas\.aliyuncs\.com))/compatible-mode/v1/?", base_url):
            raise VideoError("请填写百炼控制台提供的 HTTPS Chat Completions 服务地址（以 /compatible-mode/v1 结尾）", 422)
        if model not in MODELS:
            raise VideoError("请选择支持音视频输入的 Qwen-Omni 模型", 422)
        if key in (None, ""):
            key = self.config(owner, secret=True)["api_key"]
        if not isinstance(key, str) or not 10 <= len(key.strip()) <= 512 or any(ord(ch) < 32 for ch in key):
            raise VideoError("API Key 格式不正确", 422)
        ciphertext, nonce = encrypt_api_key(json.dumps({"api_key": key.strip(), "base_url": base_url.rstrip("/"), "model": model}), self.settings.app_master_key)
        self.repo.put_config(owner, ciphertext, nonce)
        return self.config(owner)

    def remove_config(self, owner):
        for job_id in self.repo.delete_config(owner):
            try:
                self.remove_media(job_id)
            except PermissionError:
                pass

    def capabilities(self):
        try:
            ffmpeg_executable(self.settings)
            available = True
        except VideoError:
            available = False
        return {"media_tools_available": available, "link_tools_available": importlib.util.find_spec("yt_dlp") is not None,
                "max_upload_bytes": self.settings.video_max_upload_bytes,
                "max_duration_seconds": self.settings.video_max_duration_seconds, "segment_seconds": self.settings.video_segment_seconds,
                "raw_retention_hours": self.settings.video_retention_hours, "result_retention_days": self.settings.video_result_days}

    def prepare(self, owner, filename="视频链接", source_url=None):
        self.config(owner, secret=True)
        ffmpeg_executable(self.settings)
        if source_url is not None:
            if importlib.util.find_spec("yt_dlp") is None:
                raise VideoError("请先安装 backend/requirements-video.txt 中的平台解析工具", 503)
            source_url = normalize_link(source_url)
        else:
            if not isinstance(filename, str) or Path(filename).suffix.lower() not in VIDEO_EXTENSIONS:
                raise VideoError("请选择 MP4、MOV、WebM、MKV 或 AVI 视频", 415)
        filename = re.sub(r"[\x00-\x1f/\\]", "_", filename or "视频")[:160]
        self.cleanup()
        return self.repo.create(owner, filename, source_url, self.settings.video_retention_hours)

    def upload(self, owner, stream, filename):
        job = self.prepare(owner, filename)
        directory = self.directory(job.id)
        try:
            directory.mkdir(parents=True, exist_ok=False)
            size = 0
            with self.input_path(job.id).open("xb") as output:
                while block := stream.read(65536):
                    size += len(block)
                    if size > self.settings.video_max_upload_bytes:
                        raise VideoError("视频文件超过大小限制", 413)
                    output.write(block)
            if not size:
                raise VideoError("不能上传空视频", 422)
            self.repo.uploaded(owner, job.id)
            return self.repo.get(owner, job.id)
        except BaseException:
            try:
                self.repo.delete(owner, job.id)
            except VideoError:
                pass
            self.remove_media(job.id)
            raise

    def retry(self, owner, job_id):
        job = self.repo.get(owner, job_id)
        self.config(owner, secret=True)
        if not job.source_url and not self.input_path(job.id).is_file():
            raise VideoError("原始视频已清理，请重新上传", 409)
        self.repo.retry(owner, job_id)
        return self.repo.get(owner, job_id)

    def cancel(self, owner, job_id):
        self.repo.cancel(owner, job_id)
        # API upload streams can still own an open handle on Windows; they clean up on completion.
        try:
            self.remove_media(job_id)
        except PermissionError:
            pass
        return self.repo.get(owner, job_id)

    def delete(self, owner, job_id):
        self.repo.get(owner, job_id)
        self.repo.delete(owner, job_id)
        try:
            self.remove_media(job_id)
        except PermissionError:
            pass

    def clear_workspace(self, owner):
        for job_id in self.repo.clear_workspace(owner):
            try:
                self.remove_media(job_id)
            except PermissionError:
                pass

    def cleanup(self):
        for job in self.repo.expired():
            try:
                self.remove_media(job.id)
                self.repo.delete(job.plugin_id, job.id)
            except (PermissionError, VideoError):
                pass
        # Reconcile files left by an interrupted upload or cascading workspace deletion.
        if self.root.exists():
            deadline = time.time() - self.settings.video_retention_hours * 3600
            for path in self.root.iterdir():
                if path.is_dir() and path.stat().st_mtime < deadline:
                    try:
                        self.remove_media(path.name)
                    except (VideoError, PermissionError):
                        pass


def job_response(job):
    return {"id": job.id, "filename": job.filename, "source_url": job.source_url, "status": job.status,
            "stage": job.stage, "progress": job.progress, "attempts": job.attempts,
            "created_at": job.created_at.isoformat() + "Z", "expires_at": job.expires_at.isoformat() + "Z",
            "error": job.error, "result": json.loads(job.result) if job.result else None}
