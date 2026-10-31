"""Opt-in paid video quality checks against known fixtures, using encrypted local config.

Checks semantic expectations, not merely HTTP success. Does not upload to the API,
ingest documents, or change any Workspace. Uses the production cloud adapter and
FFmpeg segmenter. Outputs contain fixture summaries/answers, never credentials.
Run --help before use. Fixtures and reports should stay in ignored local storage.
"""
from pathlib import Path
import argparse
import json
import math
import re
import sys
import tempfile
import time

try:
    from scripts.video_quality_metrics import compare_transcript, compare_reference_text, read_vtt, tokens as reference_tokens
except ModuleNotFoundError:  # Also support `python scripts/check_video_quality_live.py`.
    from video_quality_metrics import compare_transcript, compare_reference_text, read_vtt, tokens as reference_tokens

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def check_expectations(text, expected):
    """Each required group needs one alternative; forbidden phrases catch known failures."""
    failures = []
    for alternatives in expected.get("required", []):
        if not any(word in text for word in alternatives):
            failures.append({"missing_any": alternatives})
    for word in expected.get("forbidden", []):
        if word in text:
            failures.append({"unexpected": word})
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="JSON cases: file, expected, questions[{query, expected}]")
    parser.add_argument("--workspace-id", required=True, help="Existing Workspace with an independent video model config")
    parser.add_argument("--allow-paid", action="store_true", help="Explicitly enable paid cloud requests")
    parser.add_argument("--max-segments", type=int, default=1, help="Opt-in per-file segment budget, 1..40 (default: 1)")
    parser.add_argument("--max-questions", type=int, default=3, help="Opt-in per-file paid QA budget, 1..6 (default: 3); each QA may also select and verify evidence")
    parser.add_argument("--with-asr", action="store_true", help="Also pay for online ASR and compare optional reference_vtt subtitles")
    parser.add_argument("--output", type=Path, default=ROOT / ".venv/runtime/video-quality/report.json")
    args = parser.parse_args()
    if not args.allow_paid:
        parser.error("Live requests require --allow-paid; no requests sent")
    if not 1 <= args.max_segments <= 40:
        parser.error("--max-segments must be between 1 and 40")
    if not 1 <= args.max_questions <= 6:
        parser.error("--max-questions must be between 1 and 6")
    # Imports deliberately follow argument parsing so --help never touches config/services.
    from backend.core.di import get_video_service
    from backend.clients.video import CloudVideoClient
    from backend.clients.video_asr import CloudAsrClient, ASR_MODEL
    from backend.core.video_errors import VideoError
    from backend.services.video_media import inspect_video, make_segment, make_audio

    cases = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not 1 <= len(cases) <= 10:
        parser.error("Supply between 1 and 10 fixture cases")
    # Validate every input before any paid call; long fixtures require a larger explicit budget.
    service = get_video_service()
    prepared = []
    for case in cases:
        source = (args.manifest.parent / case["file"]).resolve()
        info = inspect_video(source, service.settings)
        count = math.ceil(info.duration / service.settings.video_segment_seconds)
        if count > args.max_segments:
            parser.error("Fixture exceeds --max-segments; increase the explicit paid-call budget")
        if len(case.get("questions", [])) > args.max_questions:
            parser.error("Fixture exceeds --max-questions; increase the explicit paid QA budget (maximum: 6)")
        if any(q.get("require_asr_citation") is not None and type(q["require_asr_citation"]) is not bool for q in case.get("questions", [])):
            parser.error("require_asr_citation must be a boolean")
        if not args.with_asr and any(q.get("require_asr_citation") for q in case.get("questions", [])):
            parser.error("require_asr_citation requires --with-asr")
        reference = plain_reference = None
        reference_range = case.get("reference_range_seconds", [0, info.duration])
        if (not isinstance(reference_range, list) or len(reference_range) != 2
            or any(isinstance(x, bool) or not isinstance(x, (float, int)) or not math.isfinite(x) for x in reference_range)
            or not 0 <= reference_range[0] < reference_range[1] <= info.duration):
            parser.error("reference_range_seconds must be [start, end] inside fixture")
        if case.get("reference_text") and case.get("reference_vtt"):
            parser.error("Choose reference_text or reference_vtt, not both")
        if case.get("reference_text"):
            if not args.with_asr:
                parser.error("reference_text requires --with-asr")
            plain_reference = (args.manifest.parent / case["reference_text"]).read_text(encoding="utf-8-sig")
            try:
                compare_reference_text(plain_reference, "")  # Validate size/content before any paid call.
            except ValueError as exc:
                parser.error(str(exc))
        if case.get("asr_expected") and not args.with_asr:
            parser.error("asr_expected requires --with-asr")
        if case.get("reference_vtt"):
            if not args.with_asr:
                parser.error("reference_vtt requires --with-asr")
            reference = read_vtt((args.manifest.parent / case["reference_vtt"]).read_text(encoding="utf-8-sig"))
            if len(reference_tokens(" ".join(c["text"] for c in reference if c["end_seconds"] <= info.duration))) > 12000:
                parser.error("Reference exceeds 12000 tokens")
            if not any(c["end_seconds"] <= info.duration for c in reference):
                parser.error("Reference has no complete cues inside fixture duration")
        maximum = case.get("max_token_error_rate")
        if maximum is not None and (not reference or isinstance(maximum, bool) or not isinstance(maximum, (int, float)) or not 0 <= maximum <= 1):
            parser.error("max_token_error_rate needs reference_vtt and a number in 0..1")
        maximum_cer = case.get("max_character_error_rate")
        if maximum_cer is not None and (not plain_reference or isinstance(maximum_cer, bool) or not isinstance(maximum_cer, (int, float)) or not 0 <= maximum_cer <= 1):
            parser.error("max_character_error_rate needs reference_text and a number in 0..1")
        prepared.append((case, source, info, count, reference, plain_reference, reference_range))
    config = service.config(args.workspace_id, secret=True)
    client = CloudVideoClient()
    report = {"model": config["model"], "cases": [], "passed": True,
              "scope": "Real cloud + FFmpeg; subtitle disagreement and lexical semantic checks need human review; no API/ingestion"}
    if args.with_asr:
        report["asr_model"] = ASR_MODEL
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="media-", dir=args.output.parent) as temp:
        for case, source, info, count, reference, plain_reference, reference_range in prepared:
            row = {"name": case.get("name", source.name), "has_audio": info.has_audio, "questions": []}
            started = time.monotonic()
            try:
                segment = Path(temp) / "segment.mp4"
                chapters, usage = [], {}
                transcript = {"status": "processing" if info.has_audio else "no_audio", "sentences": [], "usage": {"duration_seconds": 0}}
                for index in range(count):
                    start = index * service.settings.video_segment_seconds
                    end = min(info.duration, start + service.settings.video_segment_seconds)
                    if args.with_asr and info.has_audio:
                        audio = Path(temp) / "audio.wav"
                        try:
                            make_audio(source, audio, start, end - start, service.settings)
                            sentences, asr_usage = CloudAsrClient().transcribe(audio, end - start, config)
                            transcript["usage"]["duration_seconds"] += asr_usage.get("duration_seconds", 0)
                            for sentence in sentences:
                                transcript["sentences"].append({"id": len(transcript["sentences"]) + 1,
                                    "start_seconds": round(start + sentence["start_seconds"], 3),
                                    "end_seconds": round(start + sentence["end_seconds"], 3), "text": sentence["text"]})
                        finally:
                            audio.unlink(missing_ok=True)
                    make_segment(source, segment, start, end - start, service.settings, has_audio=info.has_audio)
                    text, tokens = client.summarize(segment, config)
                    chapters.append({"number": index + 1, "summary": text, "start_seconds": start, "end_seconds": end})
                    for key, value in tokens.items():
                        usage[key] = usage.get(key, 0) + value
                    print(f"Segment {index + 1}/{count} complete", flush=True)
                summary = chapters[0]["summary"]
                if count > 1:
                    summary, tokens = client.combine([c["summary"] for c in chapters], config)
                    for key, value in tokens.items():
                        usage[key] = usage.get(key, 0) + value
                row.update(summary=summary, usage=usage, failures=check_expectations(summary, case.get("expected", {})))
                row.update(chapters=chapters, duration_seconds=info.duration)
                if args.with_asr:
                    if info.has_audio:
                        transcript["status"] = "ready" if transcript["sentences"] else "no_speech"
                    row["transcript"] = transcript
                    original = "\n".join(s["text"] for s in transcript["sentences"])
                    row["failures"].extend({"asr": failure} for failure in check_expectations(original, case.get("asr_expected", {})))
                    if reference:
                        row["reference_metrics"] = compare_transcript(reference, transcript["sentences"], info.duration, service.settings.video_segment_seconds)
                        maximum = case.get("max_token_error_rate")
                        rate = row["reference_metrics"]["token_error_rate"]
                        if maximum is not None and rate > maximum:
                            row["failures"].append({"token_error_rate": rate, "maximum": maximum})
                    if plain_reference:
                        window = [s for s in transcript["sentences"] if s["start_seconds"] < reference_range[1] and s["end_seconds"] > reference_range[0]]
                        metrics = compare_reference_text(plain_reference, "\n".join(s["text"] for s in window))
                        row["reference_metrics"] = {**metrics, "reference_range_seconds": reference_range}
                        maximum = case.get("max_character_error_rate")
                        if maximum is not None and metrics["character_error_rate"] > maximum:
                            row["failures"].append({"character_error_rate": metrics["character_error_rate"], "maximum": maximum})
                for number, expected in case.get("chapter_expectations", {}).items():
                    matching = next((c for c in chapters if str(c["number"]) == number), None)
                    if matching is None:
                        row["failures"].append({"missing_chapter": number})
                    else:
                        row["failures"].extend({"chapter": number, **failure} for failure in check_expectations(matching["summary"], expected))
                evidence = {"overview": summary, "chapters": chapters}
                if args.with_asr and transcript["status"] == "ready":
                    evidence["transcript"] = {"basis": "云端ASR识别原文，可能误识别", "sentences": transcript["sentences"]}
                history = []
                for question in case.get("questions", []):
                    answer, tokens = client.answer(evidence, question["query"], history, config)
                    failures = check_expectations(answer, question.get("expected", {}))
                    if question.get("require_asr_citation") and not re.search(r"\[原文\s*[0-9]{1,4}", answer):
                        failures.append({"missing_asr_citation": True})
                    row["questions"].append({"query": question["query"], "answer": answer,
                                             "usage": tokens, "failures": failures})
                    history += [{"role": "user", "content": question["query"]}, {"role": "assistant", "content": answer}]
                row["passed"] = not row["failures"] and all(not q["failures"] for q in row["questions"])
            except Exception as exc:
                # Never write arbitrary upstream exception text or signed URLs.
                row.update(passed=False, error=str(exc) if isinstance(exc, VideoError) else type(exc).__name__)
            row["elapsed_seconds"] = round(time.monotonic() - started, 2)
            report["cases"].append(row)
            report["passed"] = report["passed"] and row["passed"]
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"name": row["name"], "passed": row["passed"]}, ensure_ascii=True), flush=True)
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
