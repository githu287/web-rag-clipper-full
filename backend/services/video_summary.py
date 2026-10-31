"""Video jobs and encrypted cloud configuration; previews never implicitly ingest."""
from contextlib import contextmanager
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import re
import shutil
import time
import threading
from uuid import UUID, uuid4

from ..core.security import decrypt_api_key, encrypt_api_key
from ..core.video_errors import VideoError
from ..core.video_citations import citation_ids
from ..core.video_processing import processing_view
from .video_links import normalize_link
from .video_media import ffmpeg_executable
from ..clients.video_asr import ASR_MODEL

MODELS = ("qwen3-omni-flash", "qwen3.5-omni-flash", "qwen3.5-omni-plus", "qwen3.8-omni-flash")
DEFAULT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".mkv", ".avi")


class VideoSummaryService:
    def __init__(self, repository, settings, cloud_client=None):
        self.repo = repository
        self.settings = settings
        self.root = Path(settings.upload_dir).resolve() / "video"
        self._cloud_client = cloud_client
        self._ask_lock = threading.Lock()
        self._asking = set()
        self._ask_capacity = threading.BoundedSemaphore(4)

    def ask(self, owner, job_id, payload):
        job = self.repo.get(owner, job_id)
        if job.expires_at <= datetime.utcnow():
            raise VideoError("视频总结已过期，请重新生成；已入库的内容可在知识库中问答", 409)
        if job.status != "SUCCEEDED" or not job.result:
            raise VideoError("视频总结尚未完成，暂时无法问答", 409)
        if not isinstance(payload, dict) or set(payload) - {"query", "history"}:
            raise VideoError("视频问答请求格式不正确", 422)
        query, history = payload.get("query"), payload.get("history", [])
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise VideoError("请输入 1–2000 字的问题", 422)
        if not isinstance(history, list) or len(history) > 10:
            raise VideoError("视频追问最多携带最近 10 条消息", 422)
        for message in history:
            if (not isinstance(message, dict) or set(message) != {"role", "content"}
                or message["role"] not in ("user", "assistant") or not isinstance(message["content"], str)
                or not 1 <= len(message["content"]) <= 4000):
                raise VideoError("视频问答历史格式不正确", 422)
        if sum(len(message["content"]) for message in history) > 16000:
            raise VideoError("视频问答历史过长，请清空对话后再提问", 422)
        result = json.loads(job.result)
        chapters = result.get("chapters", [])
        evidence = {"title": result["title"], "overview": result["overview"], "basis": result["basis"],
                    "timestamp_basis": "分段边界，不是精确事件时间", "chapters": [
                        {"number": index + 1, "start_seconds": chapter["start_seconds"],
                         "end_seconds": chapter["end_seconds"], "summary": chapter["summary"]}
                        for index, chapter in enumerate(chapters)]}
        transcript = result.get("transcript", {})
        sentences = transcript.get("sentences", []) if transcript.get("status") == "ready" else []
        if sentences:
            evidence["transcript"] = {"basis": "云端ASR识别原文，时间戳为识别估计，可能误识别", "sentences": sentences}
        if len(json.dumps(evidence, ensure_ascii=False)) > 100000:
            raise VideoError("视频总结过长，请选择需要的内容保存至知识库后检索问答", 413)
        config = self.config(owner, secret=True)
        with self._ask_lock:
            if owner in self._asking or not self._ask_capacity.acquire(blocking=False):
                raise VideoError("视频问答正在处理中，请稍后再试", 429)
            self._asking.add(owner)
        try:
            from ..clients.video import CloudVideoClient
            try:
                answer, usage = (self._cloud_client or CloudVideoClient()).answer(evidence, query.strip(), history, config)
            except VideoError:
                raise
            except Exception:
                raise VideoError("云端视频问答失败，请稍后重试", 502) from None
            # A deleted, expired or cancelled source must not return a stale answer.
            current = self.repo.get(owner, job_id)
            if current.expires_at <= datetime.utcnow() or current.status != "SUCCEEDED":
                raise VideoError("视频总结已不可用，问答结果已丢弃", 409)
            referenced = citation_ids(answer, "片段", set(range(1, len(chapters) + 1)))
            sources = [{"label": f"片段 {i + 1}", "start_seconds": c["start_seconds"], "end_seconds": c["end_seconds"]}
                       for i, c in enumerate(chapters) if i + 1 in referenced]
            cited = citation_ids(answer, "原文", {row["id"] for row in sentences})
            sources = [{"label": f"原文 {row['id']}", "start_seconds": row["start_seconds"],
                        "end_seconds": row["end_seconds"], "text": row["text"], "timestamp_basis": "asr_alignment"}
                       for row in sentences if row["id"] in cited] + sources
            if "[整体摘要]" in answer:
                sources.insert(0, {"label": "整体摘要", "start_seconds": None, "end_seconds": None})
            basis = "云端ASR原文与AI视频总结，未重新分析原视频" if sentences else "AI 视频总结，未重新分析原视频"
            if usage.get("retrieval_calls"):
                basis += "；已先按摘要检索最多3个片段，未命中不能判定全片不存在相关内容"
            if usage.get("verification_partial"):
                basis += "；已逐项核对引用，仅展示通过核对的结论，其余项标记无法确认；核对仍可能误判"
            elif usage.get("verification_rejected"):
                basis += "；引用不足以支持草稿结论，已停止展示该结论"
            elif usage.get("verification_calls"):
                basis += "；已用云端核对引用，仍可能误判，请核对原文"
            if usage.get("extractive_answers"):
                basis += "；确认、实体或否定范围措辞已改为服务端资料摘录，原文和AI总结分别标注；摘录是否回答问题仍需核对"
            return {"answer": answer, "sources": sources, "usage": usage, "model": config["model"], "basis": basis, "job_id": job.id}
        finally:
            with self._ask_lock:
                self._asking.discard(owner)
                self._ask_capacity.release()

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
    def working_directory(self, job=None):
        # Keep derived files under their owning job so task/Workspace removal
        # also removes files left by a hard-killed worker. Leases use disjoint
        # subdirectories; an old worker cannot remove its successor's files.
        directory = (self.directory(job.id) / ("work-" + str(UUID(job.lease)))) if job else self.directory(str(uuid4()))
        directory.mkdir(parents=True, exist_ok=False)
        try:
            yield directory
        finally:
            try:
                shutil.rmtree(directory)
            except FileNotFoundError:
                pass  # Success/cancellation may already have removed the job directory.

    def remove_media(self, job_id):
        directory = self.directory(job_id)
        if directory.exists():
            shutil.rmtree(directory)

    def config(self, owner, *, secret=False):
        row = self.repo.config(owner)
        if row is None:
            if secret:
                raise VideoError("请先在视频面板配置云端 Qwen-Omni 服务", 409)
            return {"configured": False, "base_url": DEFAULT_URL, "model": MODELS[0], "models": MODELS,
                    "asr_enabled": True, "asr_model": ASR_MODEL}
        try:
            data = json.loads(decrypt_api_key(row.ciphertext, row.nonce, self.settings.app_master_key))
        except Exception:
            raise VideoError("视频模型配置无法解密，请重新保存配置", 503) from None
        if secret:
            return data
        return {"configured": True, "base_url": data["base_url"], "model": data["model"], "models": MODELS,
                "asr_enabled": data.get("asr_enabled", False), "asr_model": ASR_MODEL}

    def put_config(self, owner, payload):
        if not isinstance(payload, dict) or set(payload) - {"api_key", "base_url", "model", "asr_enabled"}:
            raise VideoError("视频配置字段不正确", 422)
        base_url, model, key = (payload.get(field) for field in ("base_url", "model", "api_key"))
        if not isinstance(base_url, str) or not re.fullmatch(
            r"https://(?:(?:dashscope(?:-intl|-us)?\.aliyuncs\.com)|(?:[a-zA-Z0-9-]+\.(?:cn-beijing|cn-shanghai|ap-southeast-1|us-east-1)\.maas\.aliyuncs\.com))/compatible-mode/v1/?", base_url):
            raise VideoError("请填写百炼控制台提供的 HTTPS Chat Completions 服务地址（以 /compatible-mode/v1 结尾）", 422)
        if model not in MODELS:
            raise VideoError("请选择支持音视频输入的 Qwen-Omni 模型", 422)
        if "asr_enabled" in payload:
            asr_enabled = payload["asr_enabled"]
        else:
            try:
                asr_enabled = self.config(owner).get("asr_enabled", False) if self.repo.config(owner) else False
            except VideoError:
                # A fresh explicit key can repair an unreadable encrypted config.
                asr_enabled = False
        if not isinstance(asr_enabled, bool):
            raise VideoError("云端原文转写开关格式不正确", 422)
        if asr_enabled and ("-us.aliyuncs.com" in base_url or "us-east-1" in base_url or "cn-shanghai" in base_url):
            raise VideoError("云端原文转写请使用百炼北京或新加坡地域的地址与 Key", 422)
        if key in (None, ""):
            key = self.config(owner, secret=True)["api_key"]
        if not isinstance(key, str) or not 10 <= len(key.strip()) <= 512 or any(ord(ch) < 32 for ch in key):
            raise VideoError("API Key 格式不正确", 422)
        ciphertext, nonce = encrypt_api_key(json.dumps({"api_key": key.strip(), "base_url": base_url.rstrip("/"), "model": model,
                                                       "asr_enabled": asr_enabled}), self.settings.app_master_key)
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
            "error": job.error, "result": json.loads(job.result) if job.result else None,
            "processing": processing_view(job.processing_state)}
