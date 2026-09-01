"""CLI: python -m evaluation.run_baseline ..."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

from .baseline import build_report, evaluate_case, load_jsonl, validate_rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Run retrieval evaluation baseline")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    credentials = json.loads(args.credentials.read_text(encoding="utf-8"))
    all_results = []
    with httpx.Client(base_url=args.base_url, timeout=args.timeout) as client:
        for path in args.dataset:
            rows = load_jsonl(path)
            errors = validate_rows(rows, source=str(path), allow_placeholders=False)
            if errors:
                raise SystemExit("dataset validation failed:\n" + "\n".join(errors[:30]))
            for row in rows:
                plugin_id = str(row.get("target_plugin_id") or row.get("plugin_id"))
                secret_entry = credentials.get(plugin_id)
                secret = secret_entry.get("plugin_secret") if isinstance(secret_entry, dict) else secret_entry
                if not secret:
                    raise SystemExit(f"missing credential for plugin_id={plugin_id}")
                all_results.append(
                    evaluate_case(
                        client, row, dataset_name=path.name,
                        plugin_secret=str(secret), top_k=args.top_k,
                    )
                )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(build_report(all_results, args.top_k), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
