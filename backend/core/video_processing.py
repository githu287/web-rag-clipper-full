"""Versioned private checkpoints and a provider-usage ledger for one video job."""
import hashlib
import hmac
import json
import math

from .video_errors import VideoError

PIPELINE_VERSION = 1  # Bump when preprocessing or summary/ASR interpretation changes.


def load_state(raw):
    if raw is None:
        return {"version": 1, "fingerprint": None, "cache": {}, "calls": [], "reused": {}, "completed": {}}
    try:
        state = json.loads(raw)
        if (state["version"] != 1 or not isinstance(state["cache"], dict)
                or not isinstance(state["calls"], list) or len(state["calls"]) > 2500):
            raise ValueError()
        return state
    except (ValueError, TypeError, KeyError):
        raise VideoError("视频阶段记录不可用，请删除任务后重新上传", 409) from None


def dump_state(state):
    raw = json.dumps(state, ensure_ascii=False, allow_nan=False)
    if len(raw) > 2000000:
        raise VideoError("视频阶段记录超过上限，请使用较短视频", 413)
    return raw


def settle(state, status, clear=False):
    for call in state["calls"]:
        if call["status"] == "running":
            call["status"] = status
    if clear:
        state["cache"] = {}
        state["fingerprint"] = None
    return state


def reported_usage(kind, usage):
    fields = ("duration_seconds",) if kind == "asr" else ("prompt_tokens", "completion_tokens", "total_tokens")
    return {key: value for key, value in usage.items() if key in fields
            and type(value) in (int, float) and math.isfinite(value) and value >= 0}


def processing_view(raw):
    if raw is None:
        return None  # Legacy tasks have no stage ledger; never imply free calls.
    state = load_state(raw)
    known = {"video": {}, "asr": {}}
    calls = []
    for call in state["calls"]:
        usage = reported_usage(call["kind"], call.get("usage", {}))
        target = known["asr" if call["kind"] == "asr" else "video"]
        for key, value in usage.items():
            target[key] = target.get(key, 0) + value
        calls.append({key: call[key] for key in ("kind", "part", "attempt", "status")} | {
            "usage": usage, "usage_status": "reported" if usage else "unknown"})
    return {"completed": state["completed"], "reused": state["reused"], "calls": calls,
            "known_usage": known, "unknown_calls": sum(c["usage_status"] == "unknown" for c in calls),
            "untracked_attempts": state.get("untracked_attempts", 0)}


def processing_fingerprint(source, config, settings, alive, info, summary_prompt):
    media = hashlib.sha256()
    with source.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            if not alive():
                raise VideoError("任务已取消", 409)
            media.update(block)
    context = {"source": media.hexdigest(), "config": config, "pipeline": PIPELINE_VERSION,
               "segment_seconds": settings.video_segment_seconds, "media_tool": settings.video_ffmpeg, "duration": info.duration,
               "has_audio": info.has_audio, "summary_prompt": summary_prompt}
    # The fingerprint includes the key without storing its value or an unkeyed
    # credential hash. Checkpoints never leave their job/Workspace.
    return hmac.new(settings.app_master_key.encode(), json.dumps(context, sort_keys=True).encode(), hashlib.sha256).hexdigest()
