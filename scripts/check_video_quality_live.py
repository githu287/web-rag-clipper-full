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
import sys
import tempfile

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
    parser.add_argument("--output", type=Path, default=ROOT / ".venv/runtime/video-quality/report.json")
    args = parser.parse_args()
    if not args.allow_paid:
        parser.error("Live requests require --allow-paid; no requests sent")
    if not 1 <= args.max_segments <= 40:
        parser.error("--max-segments must be between 1 and 40")
    # Imports deliberately follow argument parsing so --help never touches config/services.
    from backend.core.di import get_video_service
    from backend.clients.video import CloudVideoClient
    from backend.core.video_errors import VideoError
    from backend.services.video_media import inspect_video, make_segment

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
        if len(case.get("questions", [])) > 3:
            parser.error("At most 3 questions per fixture")
        prepared.append((case, source, info, count))
    config = service.config(args.workspace_id, secret=True)
    client = CloudVideoClient()
    report = {"model": config["model"], "cases": [], "passed": True,
              "scope": "Real cloud + FFmpeg; lexical semantic checks need human review; no API/ingestion"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="media-", dir=args.output.parent) as temp:
        for case, source, info, count in prepared:
            row = {"name": case.get("name", source.name), "has_audio": info.has_audio, "questions": []}
            try:
                segment = Path(temp) / "segment.mp4"
                chapters, usage = [], {}
                for index in range(count):
                    start = index * service.settings.video_segment_seconds
                    end = min(info.duration, start + service.settings.video_segment_seconds)
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
                for number, expected in case.get("chapter_expectations", {}).items():
                    matching = next((c for c in chapters if str(c["number"]) == number), None)
                    if matching is None:
                        row["failures"].append({"missing_chapter": number})
                    else:
                        row["failures"].extend({"chapter": number, **failure} for failure in check_expectations(matching["summary"], expected))
                evidence = {"overview": summary, "chapters": chapters}
                history = []
                for question in case.get("questions", []):
                    answer, tokens = client.answer(evidence, question["query"], history, config)
                    failures = check_expectations(answer, question.get("expected", {}))
                    row["questions"].append({"query": question["query"], "answer": answer,
                                             "usage": tokens, "failures": failures})
                    history += [{"role": "user", "content": question["query"]}, {"role": "assistant", "content": answer}]
                row["passed"] = not row["failures"] and all(not q["failures"] for q in row["questions"])
            except Exception as exc:
                # Never write arbitrary upstream exception text or signed URLs.
                row.update(passed=False, error=str(exc) if isinstance(exc, VideoError) else type(exc).__name__)
            report["cases"].append(row)
            report["passed"] = report["passed"] and row["passed"]
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"name": row["name"], "passed": row["passed"]}, ensure_ascii=True), flush=True)
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
