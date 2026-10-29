"""Real Chrome extension -> video API/SQLite/FFmpeg/worker; fake cloud and ingest boundaries.

No .env, paid model calls or user's database. Owns isolated server/profile lifecycle.
"""
from pathlib import Path
import argparse
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.core.config import Settings
Settings.model_config["env_file"] = None

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import JSONResponse
from playwright.sync_api import sync_playwright, expect
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
import uvicorn

from backend.api.routers.videos import router
from backend.core.di import get_video_service, get_plugin_service
from backend.models.base import Base
from backend.models.plugin import PluginWorkspace
from backend.models.model_provider import provider_catalog
from backend.repositories.mysql.video import VideoRepository
from backend.core.video_errors import VideoError
from backend.services.video_media import ffmpeg_executable
from backend.services.video_summary import VideoSummaryService
from backend.workers.video_worker import VideoWorker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".venv/runtime/video-browser")
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    checks, uploaded_summaries, errors = [], [], []
    settings = Settings(upload_dir=str(args.artifacts / "uploads"), app_master_key="T" * 32)
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    repository = VideoRepository(engine)
    with repository.sessions.begin() as session:
        session.add(PluginWorkspace(plugin_id="video-fixture", plugin_name="Video fixture", plugin_name_norm="video fixture", plugin_secret_hash="f" * 64))
    service = VideoSummaryService(repository, settings)
    api = FastAPI()
    api.include_router(router)
    api.dependency_overrides[get_video_service] = lambda: service

    class Identity:
        def authenticate(self, plugin_id, secret):
            if plugin_id != "video-fixture" or secret != "ephemeral-video-secret":
                raise VideoError("invalid fixture identity", 401)
            return SimpleNamespace(plugin_id=plugin_id)
    api.dependency_overrides[get_plugin_service] = Identity
    @api.exception_handler(VideoError)
    async def video_error(_request, error):
        return JSONResponse({"detail": str(error), "type": "VideoError"}, status_code=error.status)
    @api.get("/plugins/me")
    def me():
        return {"plugin_id": "video-fixture", "plugin_name": "Video fixture", "api_key_configured": False}
    @api.get("/plugins/model-providers")
    def providers():
        return provider_catalog()
    @api.post("/documents/upload/async", status_code=202)
    async def ingest(file: UploadFile = File(...)):
        uploaded_summaries.append((await file.read()).decode())
        return {"id": "fixture-ingest", "status": "QUEUED"}
    @api.get("/jobs/fixture-ingest")
    def ingest_status():
        return {"id": "fixture-ingest", "status": "SUCCEEDED"}

    class CloudFixture:
        def summarize(self, *_args):
            return "主要内容：蓝色画面与提示音。\n\n关键结论：浏览器测试样例。", {"total_tokens": 18}
    media = args.artifacts / "fixture.mp4"
    subprocess.run([ffmpeg_executable(settings), "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=160x120:r=12", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000", "-t", "1", "-c:v", "libx264", "-c:a", "aac", str(media)], check=True, timeout=20)
    worker = VideoWorker(service, CloudFixture())
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base = "http://127.0.0.1:" + str(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(api, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    def done(message):
        checks.append(message)
        print(message, flush=True)
    try:
        with tempfile.TemporaryDirectory(prefix="profile-", dir=args.artifacts) as profile, sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(profile, channel=args.channel, headless=True,
                ignore_default_args=["--disable-extensions"], args=["--enable-unsafe-extension-debugging"], viewport={"width":420,"height":900})
            try:
                cdp = context.browser.new_browser_cdp_session()
                extension_id = cdp.send("Extensions.loadUnpacked", {"path": str(ROOT / "extension")})["id"]
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.add_init_script("const base = " + json.dumps(base) + ";" + """(() => {
                  const nativeFetch = window.fetch.bind(window);
                  window.fetch = (url, options) => nativeFetch(String(url).replace('http://localhost:18001', base), options);
                })();""")
                page.goto(f"chrome-extension://{extension_id}/sidepanel.html")
                page.wait_for_load_state("networkidle")
                page.evaluate("""async () => {webRagApiClient.setPluginDetails({pluginId:'video-fixture',pluginSecret:'ephemeral-video-secret',pluginName:'Video fixture'});await webRagApiClient.persistPlugin();await validatePlugin();}""")
                page.locator("#nav-video").click()
                expect(page.locator("#video-config-details")).to_have_attribute("open", "")
                page.locator("#video-key").fill("fixture-cloud-key-not-real")
                page.locator("#video-config-save").click()
                expect(page.locator("#video-config-state")).to_have_text("已保存")
                expect(page.locator("#video-key")).to_have_value("")
                assert "fixture-cloud-key" not in page.evaluate("async () => JSON.stringify(await chrome.storage.local.get(null))")
                done("Independent video configuration saves encrypted and clears the key without local persistence")
                page.locator("#video-file").set_input_files(str(media))
                page.locator("#video-upload-btn").click()
                expect(page.locator("#video-jobs .video-job")).to_have_count(1)
                assert uploaded_summaries == []
                worker.run_once()
                expect(page.locator("#video-preview")).to_be_visible(timeout=15000)
                assert "蓝色画面" in page.locator("#video-preview-text").input_value()
                assert not service.input_path(repository.list("video-fixture")[0].id).exists()
                done("Actual multipart upload, durable queue and FFmpeg reach a preview without automatic ingestion")
                expect(page.locator("#video-save")).to_be_disabled()
                page.locator("#video-preview-text").fill("# Edited video notes\nVerified content.")
                page.locator("#nav-chat").click()
                page.locator("#nav-video").click()
                expect(page.locator("#video-preview-text")).to_have_value("# Edited video notes\nVerified content.")
                page.locator("#video-confirm").check()
                page.locator("#video-save").click()
                expect(page.locator("#video-ingest-status")).to_contain_text("已保存到知识库")
                assert uploaded_summaries == ["# Edited video notes\nVerified content."]
                done("Edits survive navigation; only explicit confirmation uploads the edited Markdown to the ingest boundary")
                page.screenshot(path=str(args.artifacts / "video-panel.png"), full_page=True)
                page.reload()
                page.wait_for_load_state("networkidle")
                page.locator("#nav-video").click()
                expect(page.locator("#video-jobs .video-job")).to_have_count(1)
                page.get_by_role("button", name="查看总结", exact=True).click()
                expect(page.locator("#video-preview")).to_be_visible()
                done("Reopening the extension restores durable job history and original summary")
                page.locator("#video-url").fill("https://127.0.0.1/video/123")
                page.locator("#video-link-btn").click()
                expect(page.locator("#video-status")).to_contain_text("目前支持")
                assert len(repository.list("video-fixture")) == 1
                done("Unsupported/private links fail without creating a task")
                page.evaluate("webRagApiClient.setPluginDetails({pluginId:'other-fixture',pluginSecret:'other-secret'});webRagVideoPanel.enter()")
                expect(page.locator("#video-preview-text")).to_have_value("")
                expect(page.locator("#video-preview")).to_be_hidden()
                done("Changing Workspace clears the previous preview before asynchronous requests resolve")
                assert errors == [], errors
            finally:
                context.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        engine.dispose()
        (args.artifacts / "report.json").write_text(json.dumps({"checks": checks, "passed": len(checks) == 6, "cloud": "fixture", "ingest": "fixture boundary", "media": "real FFmpeg", "errors": errors}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
