"""Authenticated, bounded raw-image endpoint; never accepts a remote URL."""
import asyncio
from contextlib import suppress

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ...core.di import get_ocr_service
from ...models import PluginWorkspace
from ...services.ocr import MAX_IMAGE_BYTES, OcrError, OcrService
from ..deps import get_current_plugin

router = APIRouter(prefix="/ocr", tags=["ocr"])


class OcrResponse(BaseModel):
    text: str
    width: int
    height: int
    tiles: int
    warnings: list[str]
    engine: str


@router.post("/image", response_model=OcrResponse, summary="本地识别一张图片（不入库）",
             openapi_extra={"requestBody": {"required": True, "content": {
                 "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
             }}})
async def recognize_image(request: Request,
                          current_plugin: PluginWorkspace = Depends(get_current_plugin),
                          service: OcrService = Depends(get_ocr_service)):
    task = None
    try:
        if request.headers.get("content-type", "").split(";")[0].lower() not in {
            "image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp", "application/octet-stream"
        }:
            raise OcrError(415, "OCR_INVALID_IMAGE", "请发送图片二进制内容")
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_IMAGE_BYTES:
                raise OcrError(413, "OCR_TOO_LARGE", "图片超过 6 MiB 限制")
            content.extend(chunk)
        task = asyncio.create_task(service.recognize(bytes(content)))
        while not task.done():
            await asyncio.wait({task}, timeout=0.25)
            if not task.done() and await request.is_disconnected():
                raise HTTPException(499, detail={"code": "OCR_CANCELLED", "message": "已取消图片识别"})
        return await task
    except OcrError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
