"""Opt-in bounded concurrency checks on the local MySQL/API and paid video model.

Creates six temporary Workspaces and removes all of them in finally. Seeds
controlled summaries to isolate concurrency from video extraction accuracy.
Existing Workspaces/configuration/documents are never modified. No keys in reports.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import argparse
import json
import sys
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def burst(count, callback):
    barrier = threading.Barrier(count)
    def run(index):
        barrier.wait(timeout=15)
        return callback(index)
    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(run, range(count)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", required=True, help="Read-only source of an existing independent video config")
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / ".venv/runtime/video-concurrency/report.json")
    args = parser.parse_args()
    if not args.allow_paid:
        parser.error("Requires --allow-paid; no requests sent")
    import httpx
    from backend.core.di import get_video_service
    from backend.core.video_errors import VideoError
    from backend.models.video import VideoJob

    service = get_video_service()
    repo = service.repo
    config = service.config(args.workspace_id, secret=True)
    identities, report = [], {"checks": [], "passed": False, "cleanup": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with httpx.Client(base_url="http://127.0.0.1:18001", trust_env=False, timeout=120) as api:
        def register():
            name = "Concurrency " + uuid4().hex[:10]
            response = api.post("/plugins/register", json={"plugin_name": name})
            if response.status_code != 201:
                raise RuntimeError("Temporary registration failed")
            identity = response.json()
            identity.update(name=name, headers={"X-Plugin-ID": identity["plugin_id"], "X-Plugin-Secret": identity["plugin_secret"]})
            identities.append(identity)
            return identity
        def done(message):
            report["checks"].append(message)
            print(message, flush=True)
        try:
            identity = register()
            owner = identity["plugin_id"]
            def create(_):
                try:
                    repo.create(owner, "quota.mp4", None, 24)
                    return 201
                except VideoError as exc:
                    return exc.status
            statuses = burst(16, create)
            assert statuses.count(201) == 3 and statuses.count(429) == 13, statuses
            done("Real MySQL: 16 concurrent creates, exactly 3 accepted and 13 limited")
            for job in repo.list(owner):
                service.cancel(owner, job.id)
            assert all(job.status == "CANCELLED" for job in repo.list(owner))
            done("Cancelled UPLOADING tasks; no cloud calls or media files for the quota check")

            ready = []
            for index in range(5):
                identity = register()
                response = api.put("/videos/config", headers=identity["headers"], json={k: config[k] for k in ("api_key", "base_url", "model")})
                assert response.status_code == 200, "Temporary video configuration failed"
                number, job_id = 600 + index, str(uuid4())
                result = {"title": f"Controlled summary {index}", "overview": f"该空间唯一测试编号为{number}。",
                          "basis": "受控总结文本", "chapters": [{"start_seconds": 0, "end_seconds": 90, "summary": f"编号{number}"}]}
                with repo.sessions.begin() as session:
                    session.add(VideoJob(id=job_id, plugin_id=identity["plugin_id"], filename="controlled.mp4",
                        status="SUCCEEDED", stage="受控资料", expires_at=datetime.utcnow() + timedelta(hours=1), result=json.dumps(result)))
                ready.append((identity["headers"], job_id, number))
            def ask(index):
                headers, job_id, number = ready[index]
                response = api.post(f"/videos/{job_id}/ask", headers=headers,
                    json={"query": "请只回答资料中的唯一测试编号并标记来源，不使用其他信息。"})
                answer = response.json().get("answer", "")
                return {"status": response.status_code, "correct": str(number) in answer}
            same = burst(16, lambda _: ask(0))
            assert sum(r["status"] == 200 for r in same) == 1 and sum(r["status"] == 429 for r in same) == 15, same
            assert all(r["correct"] for r in same if r["status"] == 200)
            done("Real HTTP/cloud: 16 requests in one Workspace, 1 success and 15 limited")
            parallel = burst(5, ask)
            assert sum(r["status"] == 200 for r in parallel) == 4 and sum(r["status"] == 429 for r in parallel) == 1, parallel
            assert all(r["correct"] for r in parallel if r["status"] == 200)
            done("Real HTTP/cloud: 5 Workspaces, 4 successes and 1 limited; answers match each source")
            recovered = ask(0)
            assert recovered["status"] == 200 and recovered["correct"]
            assert all(api.get("/documents", headers=h).json()["total"] == 0 for h, _, _ in ready)
            done("Capacity recovered after the burst; no documents created")
            report["passed"] = True
        except Exception as exc:
            # Never persist arbitrary provider/config/HTTP exception text.
            report["error_type"] = type(exc).__name__
        finally:
            for identity in identities:
                try:
                    response = api.request("DELETE", "/plugins/me", headers=identity["headers"],
                        json={"confirm": True, "plugin_name": identity["name"]})
                    report["cleanup"].append(response.status_code == 204)
                except Exception:
                    report["cleanup"].append(False)
            report["elapsed_seconds"] = round(time.monotonic() - started, 2)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    raise SystemExit(0 if report["passed"] and all(report["cleanup"]) else 1)


if __name__ == "__main__":
    main()
