"""Two real Chrome profiles share real authenticated API/SQLite documents. No user DB or cloud calls."""
import argparse
from contextlib import ExitStack
from pathlib import Path
import json
import socket
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".venv/runtime/workspace-access-browser")
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    from backend.core.config import Settings
    Settings.model_config["env_file"] = None
    from fastapi import FastAPI
    from sqlalchemy import create_engine, select, func
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool
    from backend.models.base import Base
    from backend.models.plugin import PluginWorkspace
    from backend.models.document import Document
    from backend.repositories.mysql import PluginRepositoryImpl, DocumentRepositoryImpl
    from backend.services.plugin_service import PluginService
    from backend.api.routers import plugins, documents
    from backend.core.di import get_plugin_service, get_document_repository, get_settings
    from backend.main import _register_exception_handlers
    from playwright.sync_api import sync_playwright, expect
    import uvicorn

    servers, engines, checks, errors = [], [], [], []
    def start_backend():
        engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        engines.append(engine)
        Base.metadata.create_all(engine)
        settings = Settings(app_master_key="T" * 32)
        service = PluginService(PluginRepositoryImpl(engine), settings)
        app = FastAPI()
        app.include_router(plugins.router); app.include_router(documents.router)
        app.dependency_overrides[get_plugin_service] = lambda: service
        app.dependency_overrides[get_document_repository] = lambda: DocumentRepositoryImpl(engine)
        app.dependency_overrides[get_settings] = lambda: settings
        _register_exception_handlers(app)
        sock = socket.socket(); sock.bind(("127.0.0.1", 0))
        address = "http://127.0.0.1:" + str(sock.getsockname()[1])
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True); thread.start()
        servers.append((server, thread, sock))
        return engine, service, address

    def done(value):
        checks.append(value); print(value, flush=True)

    def counts(engine):
        with Session(engine) as session:
            return (session.scalar(select(func.count()).select_from(PluginWorkspace)), session.scalar(select(func.count()).select_from(Document)))

    try:
        engine, service, address = start_backend()
        _other_engine, _other_service, other_address = start_backend()
        with tempfile.TemporaryDirectory(dir=args.artifacts) as temp, sync_playwright() as p, ExitStack() as stack:
            def browser(index):
                context = p.chromium.launch_persistent_context(str(Path(temp) / str(index)), channel=args.channel, headless=True,
                    accept_downloads=True, viewport={"width": 420, "height": 900}, ignore_default_args=["--disable-extensions"], args=["--enable-unsafe-extension-debugging"])
                stack.callback(context.close)
                # Never let the test touch the user's live localhost backend.
                context.route("http://localhost:18001/**", lambda route: route.abort())
                eid = context.browser.new_browser_cdp_session().send("Extensions.loadUnpacked", {"path": str(ROOT / "extension")})["id"]
                def page():
                    tab = context.new_page(); tab.on("pageerror", lambda error: errors.append(str(error)))
                    tab.goto(f"chrome-extension://{eid}/sidepanel.html"); tab.wait_for_load_state("networkidle")
                    return tab
                return context, page

            first_context, first_page = browser(1)
            first = first_page()
            expect(first.locator("#view-welcome")).to_be_visible()
            first.locator("#workspace-server").fill(address)
            first.get_by_text("首次使用：创建新工作区", exact=True).click()
            first.locator("#plugin-name-input").fill("跨电脑测试知识库")
            first.locator("#plugin-register-btn").click()
            expect(first.locator("#view-app")).to_be_visible()
            identity = first.evaluate("webRagApiClient.getPlugin()")
            with Session(engine) as session, session.begin():
                session.add(Document(plugin_id=identity["pluginId"], filename="两台电脑共同资料.md", title="两台电脑共同资料", file_path="fixture-only.md", status="SUCCESS", chunk_count=1))
            outsider, _secret = service.register("另一个独立知识库")
            with Session(engine) as session, session.begin():
                session.add(Document(plugin_id=outsider.plugin_id, filename="OTHER_WORKSPACE_PRIVATE.md", file_path="fixture-other.md"))
            baseline = counts(engine)
            first.locator("#nav-library").click()
            expect(first.locator("#view-library")).to_contain_text("两台电脑共同资料")
            expect(first.locator("#view-library")).not_to_contain_text("OTHER_WORKSPACE_PRIVATE")
            first.reload(); first.wait_for_load_state("networkidle")
            expect(first.locator("#view-app")).to_be_visible()
            assert first.evaluate("webRagApiClient.getServerUrl()") == address
            done("New registration binds the chosen backend; closing/reloading restores its existing identity and isolated document list")

            first.locator("#nav-settings").click()
            first.locator("#workspace-backup-password").fill("测试登录文件的长密码2026")
            with first.expect_download() as transfer:
                first.locator("#workspace-export").click()
            backup_path = args.artifacts / "fixture-login.json"
            transfer.value.save_as(backup_path)
            content = backup_path.read_text(encoding="utf-8")
            assert identity["pluginSecret"] not in content and identity["pluginId"] not in content and "两台电脑共同资料" not in content
            expect(first.locator("#workspace-backup-password")).to_have_value("")
            done("Actual encrypted login download hides credentials and document text, and clears the file password input")

            _second_context, second_page = browser(2)
            second = second_page()
            expect(second.locator("#view-welcome")).to_be_visible()
            second.locator("#workspace-server").fill(other_address)
            second.locator("#workspace-id").fill(identity["pluginId"])
            second.locator("#workspace-secret").fill(identity["pluginSecret"])
            second.get_by_role("button", name="登录已有知识库", exact=True).click()
            expect(second.locator("#workspace-status")).to_contain_text("凭证")
            assert second.evaluate("webRagApiClient.getPlugin().pluginId") is None
            second.locator("#workspace-server").fill(address)
            second.locator("#workspace-secret").fill("wrong-key")
            second.get_by_role("button", name="登录已有知识库", exact=True).click()
            expect(second.locator("#workspace-status")).to_contain_text("凭证")
            assert counts(engine) == baseline
            done("A fresh independent Chrome profile cannot enter using the wrong backend/key and never creates an empty replacement Workspace")

            second.locator("#workspace-secret").fill(identity["pluginSecret"])
            second.get_by_role("button", name="登录已有知识库", exact=True).click()
            expect(second.locator("#view-app")).to_be_visible()
            second.locator("#nav-library").click()
            expect(second.locator("#view-library")).to_contain_text("两台电脑共同资料")
            expect(second.locator("#view-library")).not_to_contain_text("OTHER_WORKSPACE_PRIVATE")
            assert second.evaluate("webRagApiClient.getPlugin().pluginId") == identity["pluginId"] and counts(engine) == baseline
            done("Second Chrome profile logs into the exact existing Workspace and reads the same server document without copying the first profile")

            second.locator("#nav-settings").click(); second.locator("#workspace-logout").click()
            expect(second.locator("#view-welcome")).to_be_visible()
            assert counts(engine) == baseline
            expect(first.locator("#view-app")).to_be_visible()
            done("Logout removes only this browser's identity; server documents and the other computer's login remain available")
            second.get_by_text("使用加密登录文件恢复", exact=True).click()
            second.locator("#workspace-file").set_input_files(str(backup_path))
            second.locator("#workspace-file-password").fill("wrong-file-password")
            second.locator("#workspace-import").click()
            expect(second.locator("#workspace-status")).to_contain_text("文件或密码不正确")
            assert second.evaluate("webRagApiClient.getPlugin().pluginId") is None
            second.locator("#workspace-file").set_input_files(str(backup_path))
            second.locator("#workspace-file-password").fill("测试登录文件的长密码2026")
            second.locator("#workspace-import").click()
            expect(second.locator("#workspace-status")).to_contain_text("尚未连接")
            expect(second.locator("#workspace-server")).to_have_value(address)
            expect(second.locator("#workspace-file-password")).to_have_value("")
            assert second.evaluate("webRagApiClient.getPlugin().pluginId") is None
            second.get_by_role("button", name="登录已有知识库", exact=True).click()
            expect(second.locator("#view-app")).to_be_visible()
            second.locator("#nav-library").click()
            expect(second.locator("#view-library")).to_contain_text("两台电脑共同资料")
            done("Encrypted file restoration rejects a wrong password, requires explicit server confirmation, then restores the same library")

            # Existing identity must survive a failed probe and an offline restart.
            old_identity = second.evaluate("webRagApiClient.getPlugin()")
            second.evaluate("renderWelcomeView()")
            second.locator("#workspace-server").fill(other_address)
            second.locator("#workspace-id").fill(identity["pluginId"])
            second.locator("#workspace-secret").fill(identity["pluginSecret"])
            second.get_by_role("button", name="登录已有知识库", exact=True).click()
            expect(second.locator("#workspace-status")).to_contain_text("凭证")
            assert second.evaluate("webRagApiClient.getPlugin()") == old_identity
            second.context.route(address + "/**", lambda route: route.abort())
            second.reload(); second.wait_for_load_state("networkidle")
            expect(second.locator("#workspace-retry")).to_be_visible()
            assert second.evaluate("webRagApiClient.getPlugin().pluginId") == identity["pluginId"]
            second.context.unroute(address + "/**")
            second.locator("#workspace-retry").click()
            expect(second.locator("#view-app")).to_be_visible()
            done("Failed login probes and offline restart preserve saved identity; reconnect returns to the same library")

            another = first_page()
            expect(another.locator("#view-app")).to_be_visible()
            first.locator("#workspace-logout").click()
            expect(first.locator("#view-welcome")).to_be_visible()
            expect(another.locator("#view-welcome")).to_be_visible()
            expect(second.locator("#view-app")).to_be_visible()
            assert counts(engine) == baseline
            first.screenshot(path=str(args.artifacts / "login.png"), full_page=True)
            second.locator("#nav-settings").click()
            second.screenshot(path=str(args.artifacts / "settings.png"), full_page=True)
            done("All panels of one browser leave together on logout while the separate computer remains logged in; server data is unchanged")
            first.get_by_text("首次使用：创建新工作区", exact=True).click()
            first.locator("#workspace-server").fill(address)
            first.locator("#plugin-name-input").fill("保存失败后恢复的工作区")
            first.evaluate("""() => {globalThis.originalSet=chrome.storage.local.set;chrome.storage.local.set=async()=>{throw new Error('fixture storage failure')}}""")
            first.locator("#plugin-register-btn").click()
            expect(first.locator("#welcome-status")).to_contain_text("本机保存失败")
            expect(first.locator("#workspace-export-welcome")).to_be_enabled()
            first.get_by_text("使用加密登录文件恢复", exact=True).click()
            first.locator("#workspace-file-password").fill("保存失败的备份密码2026")
            with first.expect_download() as transfer:
                first.locator("#workspace-export-welcome").click()
            pending_backup = args.artifacts / "pending-fixture-login.json"
            transfer.value.save_as(pending_backup)
            first.evaluate("()=>{chrome.storage.local.set=originalSet}")
            first.locator("#workspace-file").set_input_files(str(pending_backup))
            first.locator("#workspace-file-password").fill("保存失败的备份密码2026")
            first.locator("#workspace-import").click()
            expect(first.locator("#workspace-status")).to_contain_text("尚未连接")
            first.get_by_role("button", name="登录已有知识库", exact=True).click()
            expect(first.locator("#view-app")).to_be_visible()
            assert first.evaluate("webRagApiClient.getPlugin().pluginName") == "保存失败后恢复的工作区"
            assert counts(engine) == (baseline[0] + 1, baseline[1])
            done("If registration succeeds but local storage fails, the pending credential can be encrypted/exported and restored without creating another Workspace")
            assert errors == [], errors
    finally:
        for server, thread, sock in servers:
            server.should_exit = True; thread.join(timeout=5); sock.close()
        for engine in engines: engine.dispose()
        (args.artifacts / "report.json").write_text(json.dumps({"passed": len(checks) == 9 and not errors, "checks": checks, "errors": errors,
            "auth": "real PluginService and API", "database": "isolated SQLite", "browsers": "two independent Chrome profiles", "cloud_calls": 0}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
