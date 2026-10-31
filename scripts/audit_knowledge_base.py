"""Offline operator CLI: scan by default; cleanup requires an explicit manifest.

Run from the repository root using the project's Python environment. Reports and
full vector backups may contain private data; store them outside version control.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.maintenance.knowledge_base import KnowledgeBaseMaintenance, MaintenanceError, write_json


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="只读扫描所有文档与向量，不调用收费模型")
    scan.add_argument("--output", type=Path, required=True)
    scan.add_argument("--max-rows", type=int, default=100000)
    clean = commands.add_parser("cleanup", help="按只读报告核对孤立向量，默认仅预演")
    clean.add_argument("--report", type=Path, required=True)
    clean.add_argument("--backup-dir", type=Path)
    clean.add_argument("--apply", action="store_true")
    clean.add_argument("--maintenance-window", action="store_true", help="确认API和所有Worker已暂停，且没有其他写入")
    args = parser.parse_args(argv)
    # Lazy imports keep --help independent of settings, credentials and services.
    from pymilvus import MilvusClient
    from backend.core.db import get_engine
    from backend.core.di import get_settings
    client = None
    try:
        settings = get_settings()
        client = MilvusClient(uri=f"http://{settings.milvus_host}:{settings.milvus_port}", timeout=30)
        tool = KnowledgeBaseMaintenance(get_engine(), client, settings)
        if args.command == "scan":
            report = tool.scan(args.max_rows)
            write_json(args.output, report)
            print(json.dumps({"stable": report["stable"], "documents": len(report["documents"]),
                "vectors": report["vector_rows"], "orphans": len(report["orphans"]),
                "document_anomalies": len(report["document_anomalies"]),
                "malformed_chunk_ids": len(report["malformed_chunk_ids"]), "report": str(args.output)}, ensure_ascii=False))
        else:
            report = json.loads(args.report.read_text(encoding="utf-8"))
            result = tool.cleanup(report, args.backup_dir, apply=args.apply, maintenance_window=args.maintenance_window)
            print(json.dumps({"planned": result["planned"], "deleted": len(result["deleted"]),
                              "applied": result["applied"], "backup": result.get("backup")}, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Provider/SQL exceptions can include connection details or private rows.
        print(str(exc) if isinstance(exc, MaintenanceError) else f"巡检失败（{type(exc).__name__}），未继续执行；请检查服务和本机日志", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
