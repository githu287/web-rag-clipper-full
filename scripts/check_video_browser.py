"""Real Chrome extension -> video API/SQLite/FFmpeg/worker; fake cloud and ingest boundaries.

No .env, paid model calls or user's database. Owns isolated server/profile lifecycle.
"""
from pathlib import Path
import argparse
import asyncio
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
    ingest_started, ingest_release = threading.Event(), threading.Event()
    ingest_behavior = {"next": "normal", "polls_remaining": 0}
    settings = Settings(upload_dir=str(args.artifacts / "uploads"), app_master_key="T" * 32, video_segment_seconds=10)
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    repository = VideoRepository(engine)
    with repository.sessions.begin() as session:
        session.add(PluginWorkspace(plugin_id="video-fixture", plugin_name="Video fixture", plugin_name_norm="video fixture", plugin_secret_hash="f" * 64))
        session.add(PluginWorkspace(plugin_id="other-fixture", plugin_name="Other fixture", plugin_name_norm="other fixture", plugin_secret_hash="g" * 64))
    service = VideoSummaryService(repository, settings)
    api = FastAPI()
    api.include_router(router)
    api.dependency_overrides[get_video_service] = lambda: service

    class Identity:
        def authenticate(self, plugin_id, secret):
            if {"video-fixture": "ephemeral-video-secret", "other-fixture": "other-secret"}.get(plugin_id) != secret:
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
        mode = ingest_behavior.pop("next", "normal")
        if mode.startswith("delayed-"):
            ingest_started.set()
            await asyncio.to_thread(ingest_release.wait, 10)
        if mode == "delayed-failure":
            return JSONResponse({"detail": "fixture ingest failure"}, status_code=503)
        uploaded_summaries.append((await file.read()).decode())
        ingest_behavior["polls_remaining"] = 1 if mode == "poll-once" else 0
        return {"id": "fixture-ingest", "status": "QUEUED"}
    @api.get("/jobs/fixture-ingest")
    def ingest_status():
        if ingest_behavior["polls_remaining"]:
            ingest_behavior["polls_remaining"] -= 1
            return {"id": "fixture-ingest", "status": "QUEUED"}
        return {"id": "fixture-ingest", "status": "SUCCEEDED"}

    class CloudFixture:
        def summarize(self, *_args, **options):
            if options:
                assert options["has_audio"] and options["transcript"]
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
                return "原文记录蓝色演示。[原文1-2]", {"total_tokens": 9}
            if query in ("视频只有蓝色，对吗？还讲了什么？", "视频只有蓝色，对吗；还讲了什么"):
                from backend.clients.video import CloudVideoClient
                client = CloudVideoClient()
                outputs = iter([('{"answers":{"1":"视频只有蓝色。[原文 1]","2":"蓝色演示。[原文 2]"}}', {"total_tokens": 4}),
                                ('{"checks":[{"id":2,"reason":"fixture review","supported":true}]}', {"total_tokens": 2})])
                client.generate = lambda *_a, **_kw: next(outputs)
                return client.answer(evidence, query, history, _config)
            return "蓝色背景与提示音。[片段 1]", {"total_tokens": 9}
    class AsrFixture:
        def transcribe(self, _path, duration, _config, _alive, *, keep_range, previous_tail):
            offset = keep_range[0]
            return [{"start_seconds": offset + .235, "end_seconds": offset + .835,
                     "text": "蓝色演示原文 <img src=x onerror=alert(1)>" if offset == 0 else "蓝色演示原文 第二段 Second segment 80% 请勿浇水。"}], {"duration_seconds": duration}
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
            context = playwright.chromium.launch_persistent_context(profile, channel=args.channel, headless=True, accept_downloads=True,
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
                expect(page.locator("#video-jobs .video-processing-usage")).to_contain_text("42 token")
                expect(page.locator("#video-jobs .video-stage-usage summary")).to_have_text("阶段用量记录")
                page.locator("#video-jobs .video-stage-usage summary").click()
                expect(page.locator("#video-jobs .video-stage-usage")).to_contain_text("整体摘要")
                assert "fixture-cloud-key" not in page.locator("#video-jobs").inner_text()
                done("Durable per-stage usage is visible after completion without exposing private checkpoint text or credentials")
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
                expect(page.locator("#video-captions-enabled")).to_be_disabled()
                expect(page.locator("#video-transcript-locate")).to_be_disabled()
                wrong_media = args.artifacts / "wrong-duration.webm"
                subprocess.run([ffmpeg_executable(settings), "-nostdin", "-loglevel", "error", "-y", "-protocol_whitelist", "file,pipe",
                    "-i", str(media), "-t", "1", "-c", "copy", str(wrong_media)], check=True, timeout=20)
                page.locator("#video-local-source").set_input_files(str(wrong_media))
                expect(page.locator("#video-playback-status")).to_contain_text("时长与当前任务不一致")
                expect(page.locator("#video-player")).to_be_hidden()
                expect(page.locator("#video-captions-enabled")).to_be_disabled()
                expect(page.locator("#video-transcript-locate")).to_be_disabled()
                expect(page.locator("#video-transcript-list .is-current")).to_have_count(0)
                done("Caption preview stays unavailable without a local source or when the source duration mismatches the job")
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
                expect(page.locator("#video-chat-messages .video-time-link")).to_have_count(2)
                page.locator("#video-chat-messages .video-time-link").first.click()
                assert page.locator("#video-player").evaluate("v=>v.currentTime") >= .225
                page.locator("#video-chat-messages .video-time-link").nth(1).click()
                assert page.locator("#video-player").evaluate("v=>v.currentTime") >= 10.225
                done("ASR range citation exposes both server quotes and seeks each actual local position; HTML remains text; no new upload")

                subtitle_requests, subtitle_downloads = [], []
                page.on("request", lambda request: subtitle_requests.append(request.url) if request.url.startswith(base) else None)
                page.on("download", lambda download: subtitle_downloads.append(download.suggested_filename))
                page.wait_for_load_state("networkidle")
                subtitle_requests.clear()
                preview_before = page.locator("#video-preview-text").input_value()
                counters = (len(questions), len(uploaded_summaries), len(repository.list("video-fixture")))
                search = page.locator("#video-transcript-search")
                rows = page.locator("#video-transcript-list .video-transcript-row")
                expect(search).to_be_enabled()
                page.locator("#video-player").evaluate("v=>v.pause()")
                search_position = page.locator("#video-player").evaluate("v=>v.currentTime")
                for query in ("  请勿浇水  ", "sEcOnD SeGmEnT", "80%"):
                    search.fill(query)
                    expect(page.locator("#video-transcript-search-status")).to_contain_text("找到 1 / 2")
                    expect(rows.nth(0)).to_be_hidden()
                    expect(rows.nth(1)).to_be_visible()
                search.fill("[.*]")
                expect(page.locator("#video-transcript-search-status")).to_contain_text("未找到")
                expect(page.locator("#video-transcript-list .video-transcript-row:visible")).to_have_count(0)
                search.fill("<IMG SRC=X")
                expect(rows.nth(0)).to_be_visible()
                expect(rows.nth(1)).to_be_hidden()
                expect(page.locator("#video-transcript-list img")).to_have_count(0)
                assert abs(page.locator("#video-player").evaluate("v=>v.currentTime") - search_position) < .001
                assert page.locator("#video-preview-text").input_value() == preview_before
                assert not page.locator("#video-confirm").is_checked()
                search.fill("Second")
                page.screenshot(path=str(args.artifacts / "transcript-search.png"), full_page=True)
                rows.nth(1).locator(".video-time-link").click()
                assert page.locator("#video-player").evaluate("v=>v.currentTime") >= 10.225
                page.locator("#video-transcript-search-clear").click()
                expect(search).to_have_value("")
                expect(page.locator("#video-transcript-list .video-transcript-row:visible")).to_have_count(2)
                expect(page.locator("#video-transcript-search-clear")).to_be_disabled()
                assert counters == (len(questions), len(uploaded_summaries), len(repository.list("video-fixture")))
                assert subtitle_requests == [], subtitle_requests
                done("Chinese and case-insensitive English literal search filters actual visible rows, preserves edits and playback, and keeps source-time seeking without API calls")
                with page.expect_download() as transfer:
                    page.locator("#video-subtitle-srt").click()
                transfer.value.save_as(args.artifacts / "subtitles-all.srt")
                assert transfer.value.suggested_filename.endswith(".srt")
                srt = (args.artifacts / "subtitles-all.srt").read_text(encoding="utf-8-sig")
                assert "00:00:00,235 --> 00:00:00,835" in srt and "00:00:10,235 --> 00:00:10,835" in srt
                assert "&lt;img src=x onerror=alert(1)&gt;" in srt
                with page.expect_download() as transfer:
                    page.locator("#video-subtitle-vtt").click()
                transfer.value.save_as(args.artifacts / "subtitles-all.vtt")
                vtt = (args.artifacts / "subtitles-all.vtt").read_text(encoding="utf-8")
                assert vtt.startswith("WEBVTT\n\nNOTE ") and "00:00:10.235 --> 00:00:10.835" in vtt

                def parse_track(value):
                    return page.evaluate("""value => new Promise((resolve,reject) => {
                        const video=document.getElementById('video-player'), track=document.createElement('track');
                        const url=URL.createObjectURL(new Blob([value],{type:'text/vtt'}));
                        const cleanup=()=>{clearTimeout(timer);track.remove();URL.revokeObjectURL(url);};
                        const timer=setTimeout(()=>{cleanup();reject(new Error('subtitle track timeout'));},5000);
                        track.onload=()=>{const cues=[...track.track.cues].map(c=>({start:c.startTime,end:c.endTime,text:c.getCueAsHTML().textContent}));cleanup();resolve(cues);};
                        track.onerror=()=>{cleanup();reject(new Error('subtitle track error'));};
                        track.src=url;video.append(track);track.track.mode='hidden';
                    })""", value)

                cues = parse_track(vtt)
                assert len(cues) == 2 and abs(cues[1]["start"] - 10.235) < .000001
                assert cues[0]["text"] == "蓝色演示原文 <img src=x onerror=alert(1)>"
                subprocess.run([ffmpeg_executable(settings), "-nostdin", "-loglevel", "error", "-y", "-protocol_whitelist", "file,pipe", "-i",
                    str(args.artifacts / "subtitles-all.srt"), str(args.artifacts / "subtitles-srt-roundtrip.vtt")], check=True, timeout=20)
                converted = parse_track((args.artifacts / "subtitles-srt-roundtrip.vtt").read_text(encoding="utf-8"))
                assert len(converted) == 2 and abs(converted[1]["start"] - 10.235) < .000001
                assert converted[0]["text"] == cues[0]["text"]
                assert page.locator("#video-preview-text").input_value() == preview_before
                done("Actual SRT/VTT downloads preserve fractional source times and literal text; Chrome and FFmpeg parse both formats")

                page.locator("#video-subtitle-scope").select_option("selected")
                page.locator("#video-subtitle-srt").click()
                expect(page.locator("#video-subtitle-status")).to_contain_text("请先勾选")
                assert len(subtitle_downloads) == 2
                page.locator('#video-transcript-list input[value="2"]').check()
                selection_preview = page.locator("#video-preview-text").input_value()
                page.locator("#video-confirm").check()
                search.fill("<img")
                expect(rows.nth(1)).to_be_hidden()
                expect(page.locator('#video-transcript-list input[value="2"]')).to_be_checked()
                expect(page.locator("#video-confirm")).to_be_checked()
                with page.expect_download() as transfer:
                    page.locator("#video-subtitle-vtt").click()
                transfer.value.save_as(args.artifacts / "subtitles-selected.vtt")
                chosen = parse_track((args.artifacts / "subtitles-selected.vtt").read_text(encoding="utf-8"))
                assert len(chosen) == 1 and abs(chosen[0]["start"] - 10.235) < .000001
                assert page.locator("#video-preview-text").input_value() == selection_preview
                assert counters == (len(questions), len(uploaded_summaries), len(repository.list("video-fixture")))
                assert subtitle_requests == [], subtitle_requests
                page.locator("#video-transcript-search-clear").click()
                page.locator("#video-confirm").uncheck()
                page.locator('#video-transcript-list input[value="2"]').uncheck()
                page.locator("#video-subtitle-scope").select_option("all")
                page.screenshot(path=str(args.artifacts / "subtitles.png"), full_page=True)
                done("Subtitle selection includes checked rows hidden by search and keeps source offsets; search preserves confirmation and empty selection creates no file or API request")

                player = page.locator("#video-player")
                player.evaluate("v=>v.pause()")
                locate = page.locator("#video-transcript-locate")
                current_rows = page.locator("#video-transcript-list .is-current")
                list_box = page.locator("#video-transcript-list")
                playback_notes = page.locator("#video-preview-text").input_value()
                checked_before = page.locator("#video-transcript-list input").evaluate_all("xs=>xs.map(x=>x.checked)")
                page.locator("#video-confirm").check()
                subtitle_requests.clear()
                list_box.evaluate("e=>{e.style.maxHeight='70px';e.scrollTop=0}")
                player.evaluate("v=>v.currentTime=.3")
                expect(current_rows).to_have_count(1)
                expect(current_rows.locator("input")).to_have_value("1")
                player.evaluate("v=>v.currentTime=10.4")
                expect(current_rows.locator("input")).to_have_value("2")
                assert list_box.evaluate("e=>e.scrollTop") == 0, "playback must not automatically scroll the list"
                search.fill("<img")
                expect(page.locator("#video-transcript-playback-status")).to_contain_text("被搜索隐藏")
                expect(locate).to_be_disabled()
                locate.dispatch_event("click")
                expect(search).to_have_value("<img")
                expect(rows.nth(1)).to_be_hidden()
                page.locator("#video-transcript-search-clear").click()
                expect(locate).to_be_enabled()
                locate.scroll_into_view_if_needed()
                window_position = page.evaluate("window.scrollY")
                locate.click()
                assert list_box.evaluate("e=>e.scrollTop") > 0
                assert page.evaluate("window.scrollY") == window_position
                assert rows.nth(1).locator(".video-time-link").evaluate("e=>document.activeElement===e")
                assert abs(player.evaluate("v=>v.currentTime") - 10.4) < .001 and player.evaluate("v=>v.paused")
                player.evaluate("v=>v.currentTime=5")
                expect(current_rows).to_have_count(0)
                expect(locate).to_be_disabled()
                expect(page.locator("#video-transcript-playback-status")).to_contain_text("没有对应")
                player.evaluate("v=>v.currentTime=.3")
                expect(current_rows.locator("input")).to_have_value("1")
                list_box.evaluate("e=>e.style.maxHeight=''")
                page.screenshot(path=str(args.artifacts / "transcript-playback.png"), full_page=True)
                assert page.locator("#video-preview-text").input_value() == playback_notes
                expect(page.locator("#video-confirm")).to_be_checked()
                assert page.locator("#video-transcript-list input").evaluate_all("xs=>xs.map(x=>x.checked)") == checked_before
                page.locator("#video-confirm").uncheck()
                assert subtitle_requests == [] and counters == (len(questions), len(uploaded_summaries), len(repository.list("video-fixture")))
                done("Actual media seeks highlight exact ASR rows, gaps clear highlights, search remains intact and locate scrolls only the list without changing notes, selection, confirmation or playback")
                player.evaluate("v=>{v.currentTime=.25;return v.play()}")
                expect(current_rows.locator("input")).to_have_value("1")
                page.wait_for_function("()=>document.querySelector('#video-player').currentTime>1.2")
                expect(current_rows).to_have_count(0)
                player.evaluate("v=>v.pause()")
                done("Natural local playback advances the highlight and clears it in the following ASR gap without a polling timer")
                position_before = player.evaluate("v=>v.currentTime")
                subtitle_requests.clear()
                search.fill("<img")
                page.locator("#video-captions-enabled").check()
                expect(page.locator("#video-captions-status")).to_contain_text("已显示 2 条")
                assert player.evaluate("v=>v.paused") and abs(player.evaluate("v=>v.currentTime") - position_before) < .001
                assert player.locator("track[data-asr]").count() == 1
                player.evaluate("v=>{v.pause();v.currentTime=.3}")
                page.wait_for_function("()=>{const t=document.querySelector('#video-player track[data-asr]');return t?.track.activeCues?.length===1&&Math.abs(t.track.activeCues[0].startTime-.235)<.000001}")
                assert player.evaluate("v=>v.textTracks[0].activeCues[0].getCueAsHTML().textContent") == "蓝色演示原文 <img src=x onerror=alert(1)>"
                page.screenshot(path=str(args.artifacts / "captions.png"), full_page=True)
                player.evaluate("v=>{v.pause();v.currentTime=10.4}")
                page.wait_for_function("()=>{const t=document.querySelector('#video-player track[data-asr]');return t?.track.activeCues?.length===1&&Math.abs(t.track.activeCues[0].startTime-10.235)<.000001}")
                page.locator("#video-captions-enabled").uncheck()
                expect(player.locator("track[data-asr]")).to_have_count(0)
                page.locator("#video-captions-enabled").check()
                expect(page.locator("#video-captions-status")).to_contain_text("已显示")
                player.evaluate("v=>v.textTracks[0].mode='disabled'")
                expect(page.locator("#video-captions-enabled")).not_to_be_checked()
                expect(player.locator("track[data-asr]")).to_have_count(0)
                assert subtitle_requests == [], subtitle_requests
                assert counters == (len(questions), len(uploaded_summaries), len(repository.list("video-fixture")))
                done("Real native captions follow both ASR time ranges without changing playback, uploading media or calling models; native-off clears the track")
                page.locator("#video-captions-enabled").check()
                expect(page.locator("#video-captions-status")).to_contain_text("已显示")
                page.locator("#nav-chat").click()
                expect(player.locator("track[data-asr]")).to_have_count(0)
                expect(current_rows).to_have_count(0)
                expect(locate).to_be_disabled()
                expect(page.locator("#video-captions-enabled")).not_to_be_checked()
                page.locator("#nav-video").click()
                expect(page.locator("#video-captions-enabled")).to_be_enabled()
                expect(page.locator("#video-captions-enabled")).not_to_be_checked()
                expect(search).to_have_value("<img")
                page.locator("#video-transcript-search-clear").click()
                done("Leaving the video view clears caption data; returning keeps captions off until explicitly enabled again")
                page.locator("#video-question").fill("视频只有蓝色，对吗？还讲了什么？")
                page.locator("#video-ask").click()
                expect(page.locator("#video-chat-messages .assistant")).to_have_count(2)
                last_answer = page.locator("#video-chat-messages .assistant").last
                expect(last_answer).to_contain_text("原文摘录")
                expect(last_answer).to_contain_text("2. 蓝色演示。")
                expect(last_answer).not_to_contain_text("视频只有蓝色")
                expect(last_answer.locator("img")).to_have_count(0)
                expect(page.locator("#video-chat-status")).to_contain_text("资料摘录")
                assert len(uploaded_summaries) == 1
                done("Compound question slots retain literal excerpts and verified items without publishing scoped model claims or HTML")
                page.locator("#video-question").fill("视频只有蓝色，对吗；还讲了什么")
                page.locator("#video-ask").click()
                expect(page.locator("#video-chat-messages .assistant")).to_have_count(3)
                expect(page.locator("#video-chat-messages .assistant").last).to_contain_text("2. 蓝色演示。")
                expect(page.locator("#video-chat-messages .assistant").last).not_to_contain_text("视频只有蓝色")
                done("Semicolon questions without final question marks retain two server-owned answer slots")
                for box in page.locator("#video-part-options input").all():
                    box.uncheck()
                page.locator('#video-transcript-list input[value="1"]').check()
                selected_text = page.locator("#video-preview-text").input_value()
                assert "原文 1" in selected_text and "原文 2" not in selected_text and "## 片段" not in selected_text
                search.fill("Second")
                expect(rows.nth(0)).to_be_hidden()
                assert len(uploaded_summaries) == 1
                page.locator("#video-confirm").check(); page.locator("#video-save").click()
                expect(page.locator("#video-ingest-status")).to_contain_text("已保存到知识库")
                assert uploaded_summaries[-1] == selected_text and len(uploaded_summaries) == 2
                page.locator("#video-transcript-search-clear").click()
                done("Only the selected transcript excerpt is persisted after explicit confirmation")
                def prepare_save(text, mode):
                    # Selecting a section regenerates an editable version after a
                    # previous successful save; it does not submit another file.
                    page.locator('#video-part-options input[value="overview"]').check()
                    page.locator('#video-part-options input[value="overview"]').uncheck()
                    page.locator("#video-preview-text").fill(text)
                    page.locator("#video-confirm").check()
                    ingest_started.clear(); ingest_release.clear()
                    ingest_behavior["next"] = mode

                prepare_save("# Navigation success\nKeep the upload receipt.", "delayed-success")
                page.locator("#video-save").click()
                assert ingest_started.wait(3)
                page.locator("#nav-chat").click()
                with page.expect_response("**/documents/upload/async"):
                    ingest_release.set()
                page.locator("#nav-video").click()
                expect(page.locator("#video-ingest-status")).to_contain_text("已保存到知识库")
                expect(page.locator("#video-upload-btn")).to_be_enabled()
                assert len(uploaded_summaries) == 3
                assert all(button.is_enabled() for button in page.locator("#video-jobs button").all())
                done("Saving continues across navigation and its upload receipt is recovered without duplicate submission")

                prepare_save("# Navigation failure\nKeep the editable notes.", "delayed-failure")
                page.locator("#video-save").click()
                assert ingest_started.wait(3)
                page.locator("#nav-chat").click()
                with page.expect_response("**/documents/upload/async"):
                    ingest_release.set()
                page.locator("#nav-video").click()
                expect(page.locator("#video-ingest-status")).to_contain_text("fixture ingest failure")
                expect(page.locator("#video-preview-text")).to_have_value("# Navigation failure\nKeep the editable notes.")
                expect(page.locator("#video-preview-text")).to_be_enabled()
                expect(page.locator("#video-save")).to_be_enabled()
                assert len(uploaded_summaries) == 3
                done("A failed save after navigation releases the controls and preserves the notes for a manual retry")

                prepare_save("# Poll completion\nRestore task actions.", "poll-once")
                page.locator("#video-save").click()
                expect(page.locator("#video-ingest-status")).to_contain_text("总结正在入库")
                expect(page.locator("#video-ingest-status")).to_contain_text("已保存到知识库", timeout=10000)
                assert all(button.is_enabled() for button in page.locator("#video-jobs button").all())
                expect(page.locator("#video-upload-btn")).to_be_enabled()
                done("A terminal ingestion poll restores dynamic task actions as well as the upload controls")

                prepare_save("# Workspace isolation\nDo not apply a late receipt elsewhere.", "delayed-success")
                page.locator("#video-save").click()
                assert ingest_started.wait(3)
                page.evaluate("webRagApiClient.setPluginDetails({pluginId:'other-fixture',pluginSecret:'other-secret'});webRagVideoPanel.enter()")
                with page.expect_response("**/documents/upload/async"):
                    ingest_release.set()
                expect(page.locator("#video-preview")).to_be_hidden()
                expect(page.locator("#video-preview-text")).to_have_value("")
                expect(page.locator("#video-ingest-status")).to_be_empty()
                page.evaluate("webRagApiClient.setPluginDetails({pluginId:'video-fixture',pluginSecret:'ephemeral-video-secret'});webRagVideoPanel.enter()")
                expect(page.locator("#video-jobs .video-job")).to_have_count(1)
                page.get_by_role("button", name="查看总结", exact=True).click()
                expect(page.locator("#video-preview-text")).to_be_enabled()
                done("A late save response cannot populate or lock a different Workspace")
                page.locator("#video-url").fill("https://127.0.0.1/video/123")
                page.locator("#video-link-btn").click()
                expect(page.locator("#video-status")).to_contain_text("目前支持")
                assert len(repository.list("video-fixture")) == 1
                done("Unsupported/private links fail without creating a task")
                page.locator("#video-local-source").set_input_files(str(media))
                expect(page.locator("#video-captions-enabled")).to_be_enabled()
                page.locator("#video-captions-enabled").check()
                expect(page.locator("#video-captions-status")).to_contain_text("已显示")
                search.fill("Second")
                player.evaluate("v=>{v.pause();v.currentTime=10.4}")
                expect(current_rows.locator("input")).to_have_value("2")
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
                expect(page.locator("#video-subtitle-srt")).to_be_disabled()
                expect(page.locator("#video-subtitle-vtt")).to_be_disabled()
                page.locator("#video-subtitle-vtt").dispatch_event("click")
                assert len(subtitle_downloads) == 3
                done("Workspace changes clear subtitle actions; a stale export click cannot download another Workspace's transcript")
                expect(page.locator("#video-captions-enabled")).not_to_be_checked()
                expect(page.locator("#video-captions-enabled")).to_be_disabled()
                expect(page.locator("#video-player track[data-asr]")).to_have_count(0)
                done("Workspace changes remove native caption tracks and disable preview before late responses resolve")
                expect(search).to_have_value("")
                expect(search).to_be_disabled()
                expect(page.locator("#video-transcript-search-clear")).to_be_disabled()
                expect(page.locator("#video-transcript-search-status")).not_to_contain_text("找到")
                page.locator("#video-transcript-search-clear").dispatch_event("click")
                expect(page.locator("#video-transcript-list")).to_be_empty()
                done("Workspace changes clear and disable transcript search; stale events cannot restore previous results")
                player.dispatch_event("timeupdate")
                locate.dispatch_event("click")
                expect(current_rows).to_have_count(0)
                expect(locate).to_be_disabled()
                expect(page.locator("#video-transcript-playback-status")).not_to_contain_text("原文 2")
                done("Workspace reset and late media/locate events cannot restore a previous task's active row")

                page.evaluate("webRagApiClient.setPluginDetails({pluginId:'video-fixture',pluginSecret:'ephemeral-video-secret'});webRagVideoPanel.enter()")
                expect(page.locator("#video-jobs .video-job")).to_have_count(1)
                page.get_by_role("button", name="查看总结", exact=True).click()
                search.fill("Second")
                page.locator("#video-config-details").evaluate("e=>e.open=true")
                page.locator("#video-asr-enabled").uncheck()
                page.locator("#video-config-save").click()
                expect(page.locator("#video-config-state")).to_have_text("已保存")
                page.locator("#video-file").set_input_files(str(media))
                page.locator("#video-upload-btn").click()
                expect(page.locator("#video-jobs .video-job")).to_have_count(2)
                expect(search).to_have_value("")
                expect(search).to_be_disabled()
                worker.run_once()
                expect(page.locator("#video-transcript-state")).to_contain_text("未启用原文转写", timeout=15000)
                expect(search).to_be_disabled()
                expect(page.locator("#video-transcript-list")).to_be_empty()
                expect(locate).to_be_disabled()
                expect(current_rows).to_have_count(0)
                page.get_by_role("button", name="查看总结", exact=True).nth(1).click()
                expect(search).to_be_enabled()
                expect(search).to_have_value("")
                expect(page.locator("#video-transcript-list .video-transcript-row:visible")).to_have_count(2)
                search.fill("<img")
                page.locator('#video-part-options input[value="transcript"]').check()
                expect(page.locator("#video-transcript-list input:checked")).to_have_count(2)
                page.locator('#video-part-options input[value="transcript"]').uncheck()
                expect(page.locator("#video-transcript-list input:checked")).to_have_count(0)
                done("New uploads clear search; completed tasks without ASR disable it; switching back restores all original rows without the old query")
                search.fill("Second")
                page.locator("#video-jobs .video-job").nth(1).get_by_role("button", name="删除", exact=True).click()
                expect(page.locator("#video-preview")).to_be_hidden()
                expect(search).to_have_value("")
                expect(search).to_be_disabled()
                expect(page.locator("#video-transcript-list")).to_be_empty()
                expect(locate).to_be_disabled()
                done("Deleting the selected video clears its query, result count and rows")
                page.locator("#video-config-details").evaluate("e=>e.open=true")
                page.locator("#video-provider").select_option("modelscope")
                expect(page.locator("#video-base-url")).to_have_value("https://api-inference.modelscope.cn/v1")
                expect(page.locator("#video-asr-provider")).to_have_value("groq")
                expect(page.locator("#video-asr-enabled")).to_be_checked()
                expect(page.locator("#video-asr-enabled")).to_be_disabled()
                page.locator("#video-key").fill("private-modelscope-fixture")
                page.locator("#video-asr-key").fill("private-groq-fixture")
                page.locator("#video-config-save").click()
                expect(page.locator("#video-status")).to_contain_text("分别加密保存")
                expect(page.locator("#video-key")).to_have_value("")
                expect(page.locator("#video-asr-key")).to_have_value("")
                settings_data = service.config("video-fixture", secret=True)
                assert settings_data["api_key"] == "private-modelscope-fixture"
                assert settings_data["asr_api_key"] == "private-groq-fixture"
                storage = page.evaluate("()=>new Promise(resolve=>chrome.storage.local.get(null,resolve))")
                assert "private-modelscope-fixture" not in json.dumps(storage) and "private-groq-fixture" not in json.dumps(storage)
                done("ModelScope and Groq selectors keep independent encrypted keys, require audio and clear both secret inputs")
                assert errors == [], errors
            finally:
                context.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        engine.dispose()
        late_release.set()
        ingest_release.set()
        (args.artifacts / "report.json").write_text(json.dumps({"checks": checks, "passed": len(checks) == 33, "cloud": "fixture (video and ASR)", "ingest": "fixture boundary", "media": "real FFmpeg", "errors": errors}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
