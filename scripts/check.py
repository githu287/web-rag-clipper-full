"""Run the same offline checks locally and in CI on Windows or Linux.

Use the project's Python environment; Node.js 24 is needed for extension checks.
No .env is loaded by this runner. pytest's conftest disables personal .env loading.
Dataset validation permits the public runtime-ID placeholders, not real evaluation.
"""
from __future__ import annotations

import argparse
from collections import deque
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
GROUPS = ("backend", "extension", "datasets")


def run(*args: str) -> None:
    print("+ " + " ".join(args), flush=True)
    # Keep live logs and a short failure tail visible in Actions annotations, even
    # when downloading full logs requires repository authentication.
    tail: deque[str] = deque(maxlen=80)
    with subprocess.Popen(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace",
                          env={**os.environ, "PYTHONUTF8": "1"}) as process:
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            tail.append(line)
        code = process.wait()
    if code:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            message = "".join(tail)[-8000:].replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print("::error title=Regression check failed::" + message, flush=True)
        raise subprocess.CalledProcessError(code, args)


def check_backend() -> None:
    run(sys.executable, "-m", "pytest", "-q", "--tb=short")


def check_extension() -> None:
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Node.js 24 is required for extension checks")
    # No npm/build dependency. Root JS files are the unpacked extension scripts.
    sources = sorted((ROOT / "extension").glob("*.js"))
    suites = sorted((ROOT / "extension" / "tests").glob("*.test.js"))
    if not sources or not suites:
        raise RuntimeError("No extension scripts or test suites found")
    for path in sources:
        run(node, "--check", str(path.relative_to(ROOT)))
    # Pass filenames as argv, not a shell glob; works on both Windows and Linux.
    run(node, "--test", *(str(path.relative_to(ROOT)) for path in suites))


def check_datasets() -> None:
    run(
        sys.executable, "-m", "evaluation.validate_datasets",
        "evaluation/datasets/rag_eval.jsonl",
        "evaluation/datasets/negative_eval.jsonl",
        "evaluation/datasets/isolation_eval.jsonl",
        "--allow-placeholders",
    )


def main() -> int:
    # Windows redirected consoles may default to GBK; Node's test reporter emits
    # Unicode symbols, so keep the parent log stream UTF-8 as well as children.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("groups", choices=(*GROUPS, "all"), nargs="+", help="Checks to run; 'all' runs every group")
    args = parser.parse_args()
    selected = GROUPS if "all" in args.groups else dict.fromkeys(args.groups)
    checks = {"backend": check_backend, "extension": check_extension, "datasets": check_datasets}
    try:
        for group in selected:
            checks[group]()
    except subprocess.CalledProcessError as exc:
        print(f"Check failed (exit {exc.returncode})", file=sys.stderr)
        return 1
    except (OSError, RuntimeError) as exc:
        print(f"Cannot run checks: {exc}", file=sys.stderr)
        return 1
    print("All requested checks passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
