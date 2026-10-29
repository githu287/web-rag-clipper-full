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
    checks, uploaded_summaries, errors, questions = [], [], [], []
    late_started, late_release = threading.Event(), threading.Event()
    settings = Settings(upload_dir=str(args.artifacts / "uploads"), app_master_key="T" * 32, video_segment_seconds=10)
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
        def combine(self, *_args):
            return "整体摘要：蓝色画面与声音演示。", {"total_tokens": 6}
        def answer(self, evidence, query, history, _config):
            questions.append({"evidence": evidence, "query": query, "history": history})
            if query == "延迟回答":
                late_started.set()
                late_release.wait(5)
            if query == "模拟失败":
                raise VideoError("问答失败，请重试", 502)
            if query == "原文讲了什么":
                return "原文记录蓝色演示。[原文1]", {"total_tokens": 9}
            return "蓝色背景与提示音。[片段 1]", {"total_tokens": 9}
    class AsrFixture:
        def transcribe(self, _path, duration, _config, _alive):
            return [{"start_seconds": min(.2, duration / 4), "end_seconds": min(.8, duration),
                     "text": "蓝色演示原文 <img src=x onerror=alert(1)>"}], {"duration_seconds": duration}
    service._cloud_client = CloudFixture()
    # Open codecs let the player assertions run in both installed Chrome and CI Chromium.
    media = args.artifacts / "fixture.webm"
    subprocess.run([ffmpeg_executable(settings), "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=160x120:r=12", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000", "-t", "11", "-c:v", "libvpx", "-deadline", "realtime", "-c:a", "libopus", str(media)], check=True, timeout=20)
    worker = VideoWorker(service, CloudFixture(), AsrFixture())
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
                page.locator("#video-question").fill("这是什么画面？")
                page.locator("#video-ask").click()
                expect(page.locator("#video-chat-messages .assistant")).to_have_count(1)
                assert uploaded_summaries == []
                assert "蓝色" in questions[0]["evidence"]["overview"]
                assert questions[0]["history"] == []
                page.locator("#video-question").fill("再说一下声音")
                page.locator("#video-ask").click()
                expect(page.locator("#video-chat-messages .assistant")).to_have_count(2)
                assert len(questions[1]["history"]) == 2
                assert "分段范围" in page.locator("#video-chat-messages").inner_text()
                done("Direct video questions and followups work before ingestion with summary evidence and segment references")
                page.locator("#video-question").fill("模拟失败")
                page.locator("#video-ask").click()
                expect(page.locator("#video-chat-status")).to_contain_text("问答失败")
                expect(page.locator("#video-question")).to_have_value("模拟失败")
                expect(page.locator("#video-ask")).to_be_enabled()
                done("Failed questions preserve the question and do not add a fake answer")
                expect(page.locator("#video-save")).to_be_disabled()
                page.locator('#video-part-options input[value="overview"]').uncheck()
                page.locator('#video-part-options input[value="1"]').uncheck()
                selected = page.locator("#video-preview-text").input_value()
                assert "## 片段 1" in selected and "## 片段 2" not in selected and "## 整体摘要" not in selected
                page.locator('#video-part-options input[value="0"]').uncheck()
                expect(page.locator("#video-preview-text")).to_have_value("")
                expect(page.locator("#video-save")).to_be_disabled()
                page.locator('#video-part-options input[value="0"]').check()
                done("Users choose individual sections; selecting nothing cannot persist an empty document")
                page.locator("#video-qa-title").scroll_into_view_if_needed()
                page.screenshot(path=str(args.artifacts / "video-qa.png"), full_page=True)
                page.locator("#video-preview-text").fill("# Edited video notes\nVerified content.")
                page.locator("#video-question").fill("原视频画面是什么？")
                page.locator("#video-ask").click()
                expect(page.locator("#video-chat-messages .assistant")).to_have_count(3)
                assert "Edited video notes" not in json.dumps(questions[-1]["evidence"])
                assert uploaded_summaries == []
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
                expect(page.locator("#video-transcript-list .video-transcript-row")).to_have_count(2)
                expect(page.locator("#video-transcript-list img")).to_have_count(0)
                expect(page.locator("#video-player")).to_be_hidden()
                page.locator("#video-local-source").set_input_files(str(media))
                expect(page.locator("#video-player")).to_be_visible()
                # Inspect real media state before seeking; no injected player API.
                deadline = time.monotonic() + 5
                while page.locator("#video-player").evaluate("v=>v.readyState") < 1 and time.monotonic() < deadline:
                    time.sleep(.05)
                page.locator("#video-question").fill("原文讲了什么")
                page.locator("#video-ask").click()
                expect(page.locator("#video-chat-messages .assistant")).to_have_count(1)
                expect(page.locator("#video-chat-messages")).to_contain_text("ASR时间范围")
                expect(page.locator("#video-chat-messages")).to_contain_text("蓝色演示原文")
                page.locator("#video-chat-messages .video-time-link").click()
                assert page.locator("#video-player").evaluate("v=>v.currentTime") >= .19
                done("ASR source quote seeks the actual local player; HTML remains text; reattachment creates no upload task")
                for box in page.locator("#video-part-options input").all():
                    box.uncheck()
                page.locator('#video-transcript-list input[value="1"]').check()
                selected_text = page.locator("#video-preview-text").input_value()
                assert "原文 1" in selected_text and "原文 2" not in selected_text and "## 片段" not in selected_text
                assert len(uploaded_summaries) == 1
                page.locator("#video-confirm").check(); page.locator("#video-save").click()
                expect(page.locator("#video-ingest-status")).to_contain_text("已保存到知识库")
                assert uploaded_summaries[-1] == selected_text and len(uploaded_summaries) == 2
                done("Only the selected transcript excerpt is persisted after explicit confirmation")
                page.locator("#video-url").fill("https://127.0.0.1/video/123")
                page.locator("#video-link-btn").click()
                expect(page.locator("#video-status")).to_contain_text("目前支持")
                assert len(repository.list("video-fixture")) == 1
                done("Unsupported/private links fail without creating a task")
                page.locator("#video-question").fill("延迟回答")
                page.locator("#video-ask").click()
                assert late_started.wait(3)
                page.evaluate("webRagApiClient.setPluginDetails({pluginId:'other-fixture',pluginSecret:'other-secret'});webRagVideoPanel.enter()")
                with page.expect_response("**/videos/*/ask"):
                    late_release.set()
                expect(page.locator("#video-preview-text")).to_have_value("")
                expect(page.locator("#video-preview")).to_be_hidden()
                expect(page.locator("#video-chat-messages")).to_be_empty()
                assert page.locator("#video-player").get_attribute("src") is None
                expect(page.locator("#video-transcript-list")).to_be_empty()
                done("Changing Workspace clears the previous preview before asynchronous requests resolve")
                assert errors == [], errors
            finally:
                context.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        engine.dispose()
        late_release.set()
        (args.artifacts / "report.json").write_text(json.dumps({"checks": checks, "passed": len(checks) == 11, "cloud": "fixture (Omni and ASR)", "ingest": "fixture boundary", "media": "real FFmpeg", "errors": errors}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
