"""Run separately: python -m backend.workers.video_worker. All AI is remote."""
import json
import logging
import math
import signal
import threading

from ..clients.video import CloudVideoClient
from ..clients.video_asr import CloudAsrClient, ASR_MODEL
from ..core.video_errors import VideoError
from ..services.video_links import download_platform
from ..services.video_media import inspect_video, make_segment, make_audio

logger = logging.getLogger(__name__)


def timestamp(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


class VideoWorker:
    def __init__(self, service, client=None, asr_client=None):
        self.service = service
        self.repo = service.repo
        self.settings = service.settings
        self.client = client or CloudVideoClient()
        self.asr_client = asr_client or CloudAsrClient()

    def run_once(self):
        self.service.cleanup()
        job = self.repo.claim()
        if job is None:
            return False
        stopped, cancelled = threading.Event(), threading.Event()

        def heartbeat():
            while not stopped.wait(10):
                try:
                    if not self.repo.heartbeat(job):
                        cancelled.set()
                        return
                except Exception:
                    cancelled.set()
                    return

        def alive():
            return not cancelled.is_set()

        def stage(text, progress):
            if not alive() or not self.repo.heartbeat(job, stage=text, progress=progress):
                cancelled.set()
                raise VideoError("任务已取消", 409)

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            config = self.service.config(job.plugin_id, secret=True)
            stage("读取视频素材", 3)
            # All derived media has a single cleanup scope, also on cancellation/errors.
            with self.service.working_directory() as directory:
                title = job.filename
                source = self.service.input_path(job.id)
                if job.source_url:
                    stage("解析并下载平台视频", 5)
                    source, title = download_platform(job.source_url, directory, self.settings, alive)
                elif not source.is_file():
                    raise VideoError("原始视频不存在或已过期，请重新上传")
                info = inspect_video(source, self.settings, alive)
                count = math.ceil(info.duration / self.settings.video_segment_seconds)
                chapters, usage = [], {}
                transcript = {"status": "disabled", "model": ASR_MODEL, "sentences": [], "usage": {"duration_seconds": 0}}
                if config.get("asr_enabled"):
                    transcript["status"] = "processing" if info.has_audio else "no_audio"
                for index in range(count):
                    start = index * self.settings.video_segment_seconds
                    end = min(info.duration, start + self.settings.video_segment_seconds)
                    segment = directory / "segment.mp4"
                    if transcript["status"] == "processing":
                        stage(f"云端原文转写 {index + 1}/{count}", 10 + int(75 * index / count))
                        audio = directory / "audio.wav"
                        try:
                            make_audio(source, audio, start, end - start, self.settings, alive)
                            rows, tokens = self.asr_client.transcribe(audio, end - start, config, alive)
                            transcript["usage"]["duration_seconds"] += tokens.get("duration_seconds", 0)
                            for row in rows:
                                transcript["sentences"].append({"id": len(transcript["sentences"]) + 1,
                                    "start_seconds": round(start + row["start_seconds"], 3),
                                    "end_seconds": round(start + row["end_seconds"], 3), "text": row["text"]})
                            if len(transcript["sentences"]) > 3000 or sum(len(row["text"]) for row in transcript["sentences"]) > 120000:
                                raise VideoError("转写原文过长，请使用较短视频生成可定位原文")
                        except Exception as exc:
                            if isinstance(exc, VideoError) and exc.status == 409:
                                raise
                            transcript.update(status="failed", sentences=[], error=str(exc) if isinstance(exc, VideoError) else "云端转写失败")
                        finally:
                            audio.unlink(missing_ok=True)
                    stage(f"分段转码 {index + 1}/{count}", 10 + int(75 * index / count))
                    make_segment(source, segment, start, end - start, self.settings, alive, has_audio=info.has_audio)
                    stage(f"云端音视频分析 {index + 1}/{count}", 12 + int(75 * index / count))
                    text, tokens = self.client.summarize(segment, config, alive)
                    chapters.append({"start_seconds": start, "end_seconds": round(end, 2), "summary": text})
                    for name, value in tokens.items():
                        usage[name] = usage.get(name, 0) + value
                    segment.unlink()
                if transcript["status"] == "processing":
                    transcript["status"] = "ready" if transcript["sentences"] else "no_speech"
                overview = chapters[0]["summary"]
                if count > 1:
                    stage("生成整体摘要", 90)
                    overview, tokens = self.client.combine([chapter["summary"] for chapter in chapters], config, alive)
                    for name, value in tokens.items():
                        usage[name] = usage.get(name, 0) + value
                basis = "云端模型分析视频的声音与画面" if info.has_audio else "云端模型分析画面（未检测到音轨）"
                markdown = (f"# {title}\n\n> AI 生成的视频总结，请核对原视频。\n\n"
                            f"来源：{job.source_url or job.filename}\n\n依据：{basis}；预处理为最长 {self.settings.video_segment_seconds} 秒的 720px/12fps 视频分段。"
                            "章节时间是素材分段边界，不是逐句字幕时间；细小文字和短暂画面可能遗漏。\n\n## 整体摘要\n\n" + overview)
                if count > 1:
                    markdown += "\n\n## 分段内容\n\n" + "\n\n".join(
                        f"### {timestamp(c['start_seconds'])}–{timestamp(c['end_seconds'])}\n\n{c['summary']}" for c in chapters)
                result = {"title": title, "duration_seconds": round(info.duration, 2), "has_audio": info.has_audio,
                          "basis": basis, "timestamp_basis": "segment_boundaries", "model": config["model"],
                          "overview": overview, "chapters": chapters, "usage": usage, "markdown": markdown}
                result["transcript"] = transcript
                stage("保存总结预览", 98)
                if self.repo.finish(job, result=json.dumps(result, ensure_ascii=False), result_days=self.settings.video_result_days):
                    self.service.remove_media(job.id)
        except Exception as exc:
            message = str(exc) if isinstance(exc, VideoError) else "视频处理失败，请检查媒体工具、网络或云端配置后重试"
            # Never log arbitrary provider exceptions, response bodies or signed URLs.
            logger.warning("Video job failed (%s)", type(exc).__name__)
            self.repo.finish(job, error=message[:300])
        finally:
            stopped.set()
            thread.join(timeout=2)
            try:
                current = self.repo.get(job.plugin_id, job.id)
                cleanup = current.status in ("CANCELLED", "SUCCEEDED")
            except VideoError:
                cleanup = True
            if cleanup:
                try:
                    self.service.remove_media(job.id)
                except OSError:
                    pass  # Scheduled reconciliation retries files still held by upload handles.
        return True


def main():
    from ..core.di import get_video_service
    logging.basicConfig(level=logging.INFO)
    # Signed CDN URLs must not appear in httpx's INFO request logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_args: stop.set())
    worker = VideoWorker(get_video_service())
    logger.info("Cloud video worker started")
    try:
        while not stop.is_set():
            try:
                if not worker.run_once():
                    stop.wait(2)
            except Exception as exc:
                logger.warning("Video queue unavailable (%s)", type(exc).__name__)
                stop.wait(5)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
