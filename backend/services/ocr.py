"""Ephemeral local OCR; one killable CPU child per API process, no URL fetching."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys

MAX_IMAGE_BYTES = 6 * 1024 * 1024
OCR_TIMEOUT_SECONDS = 40


class OcrError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


class OcrService:
    def __init__(self):
        self._lock = asyncio.Lock()

    async def recognize(self, content: bytes) -> dict:
        if len(content) > MAX_IMAGE_BYTES:
            raise OcrError(413, "OCR_TOO_LARGE", "图片超过 6 MiB 限制")
        if not content:
            raise OcrError(415, "OCR_INVALID_IMAGE", "图片内容为空")
        if any(importlib.util.find_spec(name) is None for name in ("rapidocr_onnxruntime", "PIL", "onnxruntime")):
            raise OcrError(503, "OCR_UNAVAILABLE", "本地 OCR 未安装，请安装 backend/requirements-ocr.txt 后重启后端")
        if self._lock.locked():
            raise OcrError(429, "OCR_BUSY", "本地 OCR 正在处理其他图片，请稍后重试")
        async with self._lock:
            # Do not forward workspace/model secrets or load .env in the child.
            env = {key: value for key, value in os.environ.items()
                   if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LD_LIBRARY_PATH"}}
            env.update(PYTHONUTF8="1", OMP_NUM_THREADS="2")
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "backend.ocr_worker",
                cwd=Path(__file__).resolve().parents[2], env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                **({"creationflags": 0x08000000} if sys.platform == "win32" else {}),
            )
            try:
                try:
                    output, _ = await asyncio.wait_for(process.communicate(content), OCR_TIMEOUT_SECONDS)
                except TimeoutError as exc:
                    raise OcrError(504, "OCR_TIMEOUT", "图片识别超过 40 秒，请尝试较小的图片或区域") from exc
                if process.returncode or len(output) > 256000:
                    raise OcrError(503, "OCR_FAILED", "本地图片识别失败")
                try:
                    result = json.loads(output)
                except (ValueError, UnicodeError) as exc:
                    raise OcrError(503, "OCR_FAILED", "本地图片识别失败") from exc
                if "error" in result:
                    raise OcrError(415, "OCR_INVALID_IMAGE", result["error"])
                return result
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
