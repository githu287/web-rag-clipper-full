"""Bounded CPU-only media preprocessing. No local inference or network protocols."""
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess
import time

from ..core.video_errors import VideoError


def ffmpeg_executable(settings):
    found = shutil.which(settings.video_ffmpeg)
    if found:
        return found
    if settings.video_ffmpeg != "ffmpeg":
        raise VideoError("配置的 FFmpeg 不可用", 503)
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        raise VideoError("请先安装 backend/requirements-video.txt 中的媒体工具", 503) from None


def run_media(args, *, timeout, alive=lambda: True):
    """Drain a small bounded diagnostic tail; allow cancel during decoding, with no shell."""
    import tempfile
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        started = time.monotonic()
        try:
            while process.poll() is None:
                if not alive():
                    raise VideoError("任务已取消", 409)
                if time.monotonic() - started > timeout or log.tell() > 262144:
                    raise VideoError("媒体处理超时或文件无法解析，请换用 MP4 文件")
                time.sleep(0.15)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        log.seek(0)
        output = log.read(262144).decode("utf-8", errors="replace")
        if process.returncode:
            raise VideoError("视频损坏、格式不支持或解码失败，请换用 MP4 文件")
        return output


@dataclass(frozen=True)
class VideoInfo:
    duration: float
    has_audio: bool


def inspect_video(path: Path, settings, alive=lambda: True):
    # Reject playlist containers even with a spoofed .mp4 extension. FFmpeg sees only local bytes.
    with path.open("rb") as stream:
        header = stream.read(64)
    if not (header[4:8] == b"ftyp" or header.startswith(b"\x1aE\xdf\xa3") or header.startswith(b"RIFF")):
        raise VideoError("请选择 MP4、MOV、WebM、MKV 或 AVI 视频文件")
    text = run_media([ffmpeg_executable(settings), "-nostdin", "-hide_banner", "-protocol_whitelist", "file,pipe",
                      "-i", str(path), "-map", "0:v:0", "-t", "0", "-f", "null", "-"], timeout=20, alive=alive)
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not match or "Video:" not in text:
        raise VideoError("无法读取视频时长或没有视频画面")
    hours, minutes, seconds = map(float, match.groups())
    duration = hours * 3600 + minutes * 60 + seconds
    if not 0 < duration <= settings.video_max_duration_seconds:
        raise VideoError(f"视频必须大于 0 秒且不超过 {settings.video_max_duration_seconds // 60} 分钟")
    return VideoInfo(duration, "Audio:" in text)


def make_segment(source, target, start, length, settings, alive=lambda: True, *, has_audio=True):
    # Qwen-Omni rejects some video-only MP4s. A bounded silent track makes the
    # container compatible; callers retain the original has_audio metadata.
    audio_input = [] if has_audio else ["-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono"]
    audio_map = "0:a:0?" if has_audio else "1:a:0"
    run_media([ffmpeg_executable(settings), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
               "-protocol_whitelist", "file,pipe", "-ss", str(start), "-i", str(source), *audio_input, "-t", str(length),
               "-map", "0:v:0", "-map", audio_map, *([] if has_audio else ["-shortest"]), "-vf", "scale='min(720,iw)':-2,fps=12",
               "-c:v", "libx264", "-preset", "veryfast", "-b:v", "450k", "-maxrate", "600k", "-bufsize", "1200k",
               "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-threads", "2", "-movflags", "+faststart", str(target)],
              timeout=180, alive=alive)
    if not target.is_file() or not 0 < target.stat().st_size <= 7 * 1024 * 1024:
        raise VideoError("视频分段过大或无法编码")
