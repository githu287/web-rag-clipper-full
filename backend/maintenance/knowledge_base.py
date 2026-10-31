"""Read-only vector reconciliation and manifest-bound orphan cleanup."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from numbers import Real
import os
from pathlib import Path

from sqlalchemy import select

from ..models.document import Document
from ..models.ingest_job import IngestJob

FIELDS = ["id", "page_id", "chunk_index", "chunk_text"]
FULL_FIELDS = [*FIELDS, "embedding"]


class MaintenanceError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def write_json(path, value):
    """Never overwrite an earlier report or backup."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(canonical(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def sql_snapshot(engine):
    # Separate transactions per call: a repeated MySQL snapshot would hide a
    # concurrent document created while the vector iterator was running.
    with engine.connect() as connection:
        documents = [dict(r) for r in connection.execute(select(
            Document.id, Document.status, Document.chunk_count)).mappings()]
        active = list(connection.execute(select(IngestJob.id).where(
            IngestJob.status.in_(["QUEUED", "RUNNING"]))).scalars())
    return {"documents": sorted(documents, key=lambda r: r["id"]), "active_jobs": sorted(active)}


class KnowledgeBaseMaintenance:
    def __init__(self, engine, client, settings):
        self.engine, self.client = engine, client
        self.collection = settings.milvus_collection
        self.dimension = settings.bailian_embedding_dimension
        # No password, user, token or decrypted configuration in reports.
        url = engine.url
        self.target = {
            "database": digest([url.drivername, url.host, url.port, url.database]),
            "milvus_uri": f"http://{settings.milvus_host}:{settings.milvus_port}",
            "collection": self.collection, "dimension": self.dimension,
        }

    @staticmethod
    def metadata(row):
        if (not isinstance(row.get("id"), str) or not 0 < len(row["id"]) <= 64
                or type(row.get("page_id")) is not int or row["page_id"] < 0
                or type(row.get("chunk_index")) is not int or row["chunk_index"] < 0
                or not isinstance(row.get("chunk_text"), str)):
            raise MaintenanceError("向量字段无效，已停止；没有生成可清理清单")
        return {k: row[k] for k in FIELDS}

    def scan(self, max_rows=100000):
        if not 1 <= max_rows <= 1000000:
            raise MaintenanceError("max_rows 必须在1至1000000之间")
        before = sql_snapshot(self.engine)
        documents = {r["id"]: r for r in before["documents"]}
        counts, indexes, orphans, malformed, seen = Counter(), defaultdict(set), [], [], set()
        iterator = self.client.query_iterator(
            collection_name=self.collection, filter="", output_fields=FIELDS,
            batch_size=500, consistency_level="Strong", timeout=30)
        try:
            while batch := iterator.next():
                for row in batch:
                    row = self.metadata(row)
                    if row["id"] in seen or len(seen) >= max_rows:
                        raise MaintenanceError("扫描出现重复主键或超过行数上限，结果不完整；请调整后重新扫描")
                    seen.add(row["id"])
                    page = row["page_id"]
                    counts[page] += 1
                    indexes[page].add(row["chunk_index"])
                    if row["id"] != f"{page}_{row['chunk_index']}":
                        malformed.append(row["id"])
                    if page not in documents:
                        orphans.append({"id": row["id"], "page_id": page,
                                        "chunk_index": row["chunk_index"], "sha256": digest(row)})
        finally:
            iterator.close()
        anomalies = []
        for page, document in documents.items():
            actual, expected = counts[page], document["chunk_count"]
            if document["status"] == "SUCCESS":
                ordered = indexes[page]
                contiguous = (not ordered and expected == 0) or (
                    len(ordered) == expected and min(ordered) == 0 and max(ordered) == expected - 1)
                if actual != expected or not contiguous or actual == 0:
                    anomalies.append({**document, "actual_chunks": actual, "kind": "missing_or_mismatched_chunks"})
            elif actual:
                anomalies.append({**document, "actual_chunks": actual, "kind": "non_success_document_vectors"})
        after = sql_snapshot(self.engine)
        stable = before == after and not before["active_jobs"] and not any(
            r["status"] in ("PROCESSING", "DELETING") for r in before["documents"])
        return {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                "target": self.target, "stable": stable, "documents": before["documents"],
                "active_job_count": len(before["active_jobs"]), "vector_rows": len(seen),
                "orphans": sorted(orphans, key=lambda r: r["id"]),
                "document_anomalies": anomalies, "malformed_chunk_ids": sorted(malformed)}

    def _idle(self, candidates):
        snapshot = sql_snapshot(self.engine)
        if snapshot["active_jobs"] or any(r["status"] in ("PROCESSING", "DELETING") for r in snapshot["documents"]):
            raise MaintenanceError("存在排队/运行/删除中的任务，请停止写入并等待任务结束后再清理")
        referenced = {r["id"] for r in snapshot["documents"]}
        if referenced.intersection(r["page_id"] for r in candidates):
            raise MaintenanceError("清单中的文档已存在，停止清理；请重新扫描")

    def _rows(self, candidates, fields):
        rows = []
        for offset in range(0, len(candidates), 100):
            ids = [r["id"] for r in candidates[offset:offset + 100]]
            rows.extend(self.client.query(collection_name=self.collection, ids=ids,
                                          output_fields=fields, consistency_level="Strong", timeout=30))
        if "embedding" in fields:
            normalized = []
            for row in rows:
                vector = row.get("embedding")
                # PyMilvus 2.4 returns numpy.float32 values. Convert them to
                # exact Python float representations for a portable JSON backup.
                if (not isinstance(vector, list) or len(vector) != self.dimension
                        or any(isinstance(v, bool) or not isinstance(v, Real) or not math.isfinite(v) for v in vector)):
                    raise MaintenanceError("无法导出完整有效向量，停止清理")
                normalized.append({**self.metadata(row), "embedding": [float(v) for v in vector]})
            return normalized
        return rows

    def cleanup(self, report, backup_dir=None, *, apply=False, maintenance_window=False):
        if report.get("version") != 1 or report.get("target") != self.target or report.get("stable") is not True:
            raise MaintenanceError("报告版本、目标或稳定性不匹配，请重新执行只读扫描")
        candidates = report.get("orphans")
        if not isinstance(candidates, list):
            raise MaintenanceError("孤立向量清单无效")
        ids = [r.get("id") for r in candidates]
        if (any(not isinstance(i, str) or not 0 < len(i) <= 64 for i in ids) or len(ids) != len(set(ids))
                or any(type(r.get("page_id")) is not int or r["page_id"] < 0 for r in candidates)):
            raise MaintenanceError("清单主键或文档编号无效")
        self._idle(candidates)
        current = self._rows(candidates, FIELDS)
        expected = {r["id"]: r["sha256"] for r in candidates}
        listed = {r["id"]: (r["page_id"], r.get("chunk_index")) for r in candidates}
        if ({r["id"]: digest(self.metadata(r)) for r in current} != expected
                or any(listed.get(r["id"]) != (r["page_id"], r["chunk_index"]) for r in current)):
            raise MaintenanceError("向量已变化或缺失，停止清理；请重新扫描")
        result = {"target": self.target, "planned": len(ids), "deleted": [], "applied": apply}
        if not apply or not candidates:
            return result
        if not maintenance_window or backup_dir is None:
            raise MaintenanceError("实际清理需要 --maintenance-window 和新的备份目录；先暂停API与所有Worker")
        rows = self._rows(candidates, FULL_FIELDS)
        if len(rows) != len(candidates) or {r["id"]: digest(self.metadata(r)) for r in rows} != expected:
            raise MaintenanceError("备份读取与清单不一致，停止清理")
        backup = Path(backup_dir)
        backup.mkdir(parents=True, exist_ok=False)
        write_json(backup / "scan.json", report)
        write_json(backup / "vectors.json", rows)
        # Read back the durable file before any destructive call.
        restored = json.loads((backup / "vectors.json").read_text(encoding="utf-8"))
        if digest(restored) != digest(rows):
            raise MaintenanceError("备份回读校验失败，停止清理")
        write_json(backup / "manifest.json", {"version": 1, "target": self.target, "rows": len(rows),
                   "vectors_sha256": digest(restored), "scan_sha256": digest(report),
                   "created_at": datetime.now(timezone.utc).isoformat()})
        result["backup"] = str(backup.resolve())
        try:
            for offset in range(0, len(candidates), 100):
                batch = candidates[offset:offset + 100]
                self._idle(batch)
                batch_ids = [r["id"] for r in batch]
                current = self._rows(batch, FULL_FIELDS)
                backed_up = {r["id"]: digest(r) for r in restored if r["id"] in batch_ids}
                if {r["id"]: digest(r) for r in current} != backed_up:
                    raise MaintenanceError("备份后向量发生变化，停止清理")
                self.client.delete(collection_name=self.collection, ids=batch_ids, timeout=30)
                if self._rows(batch, ["id"]):
                    raise MaintenanceError("删除后仍可读到目标向量，请保留备份并检查")
                result["deleted"].extend(batch_ids)
            result["status"] = "completed"
        except Exception:
            result["status"] = "interrupted"
            raise
        finally:
            write_json(backup / "cleanup-result.json", result)
        return result
