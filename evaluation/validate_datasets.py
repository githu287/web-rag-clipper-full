"""Validate evaluation JSONL structure without contacting Backend."""

from __future__ import annotations

import argparse
from pathlib import Path

from .baseline import load_jsonl, validate_rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path, nargs="+")
    parser.add_argument("--allow-placeholders", action="store_true")
    args = parser.parse_args()
    errors: list[str] = []
    count = 0
    for path in args.dataset:
        rows = load_jsonl(path)
        count += len(rows)
        errors.extend(
            validate_rows(
                rows, source=str(path), allow_placeholders=args.allow_placeholders
            )
        )
    if errors:
        print("\n".join(errors))
        return 1
    print(f"validated {count} cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
