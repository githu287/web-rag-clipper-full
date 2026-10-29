"""Authenticated video setup, durable jobs and editable preview results."""
import asyncio
import json
from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import Response

from ...core.di import get_video_service
from ...models.plugin import PluginWorkspace
from ...core.video_errors import VideoError
from ...services.video_summary import VideoSummaryService, job_response
from ..deps import get_current_plugin

router = APIRouter(prefix="/videos", tags=["videos"])


async def read_json(request, limit):
    body = bytearray()
    async for block in request.stream():
        if len(body) + len(block) > limit:
            raise VideoError("视频请求过大", 413)
        body.extend(block)
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise VideoError("视频请求不是有效 JSON", 422) from None


@router.get("/config")
def config(plugin: PluginWorkspace = Depends(get_current_plugin), service: VideoSummaryService = Depends(get_video_service)):
    return {**service.config(plugin.plugin_id), **service.capabilities()}


@router.put("/config")
async def put_config(request: Request, plugin: PluginWorkspace = Depends(get_current_plugin), service: VideoSummaryService = Depends(get_video_service)):
    # Avoid request-validation responses reflecting API keys back to clients.
    data = await read_json(request, 4096)
    return await asyncio.to_thread(service.put_config, plugin.plugin_id, data)


@router.delete("/config", status_code=204)
def delete_config(plugin: PluginWorkspace = Depends(get_current_plugin), service: VideoSummaryService = Depends(get_video_service)):
    service.remove_config(plugin.plugin_id)
    return Response(status_code=204)


@router.get("")
def list_jobs(plugin: PluginWorkspace = Depends(get_current_plugin), service: VideoSummaryService = Depends(get_video_service)):
    return {"items": [job_response(job) for job in service.repo.list(plugin.plugin_id)]}


@router.post("/upload", status_code=202)
def upload(file: UploadFile = File(...), plugin: PluginWorkspace = Depends(get_current_plugin), service: VideoSummaryService = Depends(get_video_service)):
    try:
        return job_response(service.upload(plugin.plugin_id, file.file, file.filename))
    finally:
        file.file.close()


@router.post("/link", status_code=202)
async def link(request: Request, plugin: PluginWorkspace = Depends(get_current_plugin), service: VideoSummaryService = Depends(get_video_service)):
    data = await read_json(request, 8192)
    try:
        source = data["url"]
    except (ValueError, TypeError, KeyError):
        raise VideoError("请输入有效视频链接", 422) from None
    if not isinstance(source, str):
        raise VideoError("请输入有效视频链接", 422)
    job = await asyncio.to_thread(service.prepare, plugin.plugin_id, source_url=source)
    return job_response(job)


@router.get("/{job_id}")
def get_job(job_id: str, plugin: PluginWorkspace = Depends(get_current_plugin), service: VideoSummaryService = Depends(get_video_service)):
    return job_response(service.repo.get(plugin.plugin_id, job_id))


@router.post("/{job_id}/ask")
async def ask(job_id: str, request: Request, plugin: PluginWorkspace = Depends(get_current_plugin), service: VideoSummaryService = Depends(get_video_service)):
    payload = await read_json(request, 80000)
    return await asyncio.to_thread(service.ask, plugin.plugin_id, job_id, payload)


@router.post("/{job_id}/retry", status_code=202)
def retry(job_id: str, plugin: PluginWorkspace = Depends(get_current_plugin), service: VideoSummaryService = Depends(get_video_service)):
    return job_response(service.retry(plugin.plugin_id, job_id))


@router.post("/{job_id}/cancel")
def cancel(job_id: str, plugin: PluginWorkspace = Depends(get_current_plugin), service: VideoSummaryService = Depends(get_video_service)):
    return job_response(service.cancel(plugin.plugin_id, job_id))


@router.delete("/{job_id}", status_code=204)
def delete_job(job_id: str, plugin: PluginWorkspace = Depends(get_current_plugin), service: VideoSummaryService = Depends(get_video_service)):
    service.delete(plugin.plugin_id, job_id)
    return Response(status_code=204)
