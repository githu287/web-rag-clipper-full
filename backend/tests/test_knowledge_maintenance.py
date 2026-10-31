"""Reconciliation must distinguish missing data and protect current documents."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, insert

from backend.maintenance.knowledge_base import KnowledgeBaseMaintenance, MaintenanceError, digest, sql_snapshot
from backend.models.base import Base
from backend.models.document import Document
from backend.models.ingest_job import IngestJob
from backend.models.plugin import PluginWorkspace  # register FK metadata


def vector(page, index=0):
    return {"id": f"{page}_{index}", "page_id": page, "chunk_index": index,
            "chunk_text": "private original text", "embedding": [.25, -.5]}


class Vectors:
    def __init__(self, rows):
        self.rows = {r["id"]: deepcopy(r) for r in rows}
        self.deleted, self.closed = [], False
        self.on_iterate = lambda: None
        self.on_query = lambda: None
        self.on_delete = lambda: None

    def query_iterator(self, **options):
        assert options["consistency_level"] == "Strong" and "embedding" not in options["output_fields"]
        batches = iter([[{k: r[k] for k in options["output_fields"]}] for r in self.rows.values()] + [[]])
        def next_batch():
            self.on_iterate()
            return next(batches)
        return SimpleNamespace(next=next_batch, close=lambda: setattr(self, "closed", True))

    def query(self, **options):
        assert options["consistency_level"] == "Strong" and options["ids"]
        self.on_query()
        return [{k: r[k] for k in options["output_fields"]} for r in self.rows.values() if r["id"] in options["ids"]]

    def delete(self, **options):
        self.on_delete()
        self.deleted.extend(options["ids"])
        for key in options["ids"]:
            self.rows.pop(key, None)


@pytest.fixture
def setup_tool():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    settings = SimpleNamespace(milvus_collection="fixture", bailian_embedding_dimension=2,
                               milvus_host="localhost", milvus_port=19530)
    client = Vectors([vector(7), vector(8)])
    tool = KnowledgeBaseMaintenance(engine, client, settings)
    yield tool, engine, client
    engine.dispose()


def document(engine, page, status="SUCCESS", count=1):
    with engine.begin() as connection:
        connection.execute(insert(Document).values(id=page, plugin_id="fixture", filename="x",
                           file_path="", status=status, chunk_count=count))


def test_scan_separates_orphans_missing_vectors_counts_and_non_success_data(setup_tool):
    tool, engine, client = setup_tool
    document(engine, 7, count=2)
    document(engine, 9)
    document(engine, 10, "FAILED")
    client.rows["10_0"] = vector(10)
    report = tool.scan()
    assert report["stable"] and report["vector_rows"] == 3
    assert [r["id"] for r in report["orphans"]] == ["8_0"]
    assert {r["id"]: r["kind"] for r in report["document_anomalies"]} == {
        7: "missing_or_mismatched_chunks", 9: "missing_or_mismatched_chunks", 10: "non_success_document_vectors"}
    assert "private original text" not in json.dumps(report) and "embedding" not in json.dumps(report)
    assert client.closed and not client.deleted


def test_scan_detects_wrong_index_even_when_counts_match(setup_tool):
    tool, engine, client = setup_tool
    document(engine, 7)
    client.rows["7_0"]["chunk_index"] = 3
    report = tool.scan()
    assert report["malformed_chunk_ids"] == ["7_0"]
    assert report["document_anomalies"][0]["id"] == 7


def test_dry_run_does_not_write_backups_or_delete(setup_tool, tmp_path):
    tool, engine, client = setup_tool
    report = tool.scan()
    result = tool.cleanup(report, tmp_path / "backup")
    assert result["planned"] == 2 and not result["applied"]
    assert not client.deleted and not (tmp_path / "backup").exists()
    assert not sql_snapshot(engine)["documents"]


def test_full_backup_is_verified_before_exact_id_deletion_and_current_documents_survive(setup_tool, tmp_path):
    tool, engine, client = setup_tool
    document(engine, 7)
    report = tool.scan()
    backup = tmp_path / "backup"
    original = deepcopy(client.rows["8_0"])
    def verify_backup():
        data = json.loads((backup / "vectors.json").read_text(encoding="utf-8"))
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        assert data == [original] and manifest["vectors_sha256"] == digest(data)
    client.on_delete = verify_backup
    result = tool.cleanup(report, backup, apply=True, maintenance_window=True)
    assert result["status"] == "completed" and client.deleted == ["8_0"]
    assert client.rows == {"7_0": vector(7)}
    assert sql_snapshot(engine)["documents"][0]["id"] == 7
    assert json.loads((backup / "cleanup-result.json").read_text())["deleted"] == ["8_0"]


@pytest.mark.parametrize("change", ["text", "missing", "document", "target", "unstable"])
def test_stale_or_wrong_target_reports_cannot_delete(setup_tool, tmp_path, change):
    tool, engine, client = setup_tool
    report = tool.scan()
    if change == "text": client.rows["8_0"]["chunk_text"] = "changed"
    if change == "missing": del client.rows["8_0"]
    if change == "document": document(engine, 8)
    if change == "target": report["target"] = {"collection": "other"}
    if change == "unstable": report["stable"] = False
    with pytest.raises(MaintenanceError):
        tool.cleanup(report, tmp_path / "backup", apply=True, maintenance_window=True)
    assert not client.deleted


@pytest.mark.parametrize("active", ["PROCESSING", "DELETING", "QUEUED", "RUNNING"])
def test_active_operations_block_cleanup_and_mark_scan_unstable(setup_tool, tmp_path, active):
    tool, engine, client = setup_tool
    report = tool.scan()
    if active in ("PROCESSING", "DELETING"):
        document(engine, 99, active)
    else:
        with engine.begin() as connection:
            connection.execute(insert(IngestJob).values(id="active", plugin_id="fixture", job_type="WEB_CLIP", status=active))
    assert not tool.scan()["stable"]
    with pytest.raises(MaintenanceError):
        tool.cleanup(report, tmp_path / "backup", apply=True, maintenance_window=True)
    assert not client.deleted


def test_document_created_during_scan_prevents_stable_cleanup_report(setup_tool):
    tool, engine, client = setup_tool
    def create_once():
        if not sql_snapshot(engine)["documents"]:
            document(engine, 8)
    client.on_iterate = create_once
    report = tool.scan()
    assert not report["stable"] and not client.deleted


def test_actual_pymilvus_float32_values_are_backed_up_without_precision_loss(setup_tool, tmp_path):
    import numpy as np
    tool, _, client = setup_tool
    client.rows["7_0"]["embedding"] = [np.float32(.123456), np.float32(-.987654)]
    report = tool.scan()
    tool.cleanup(report, tmp_path / "backup", apply=True, maintenance_window=True)
    rows = json.loads((tmp_path / "backup/vectors.json").read_text())
    assert rows[0]["embedding"] == [float(np.float32(.123456)), float(np.float32(-.987654))]
    assert [np.float32(x) for x in rows[0]["embedding"]] == [np.float32(.123456), np.float32(-.987654)]


@pytest.mark.parametrize("vector_value", [None, [0.1], [float("nan"), 0], [True, .1]])
def test_incomplete_backup_cannot_delete(setup_tool, tmp_path, vector_value):
    tool, _, client = setup_tool
    report = tool.scan()
    client.rows["8_0"]["embedding"] = vector_value
    with pytest.raises(MaintenanceError):
        tool.cleanup(report, tmp_path / "backup", apply=True, maintenance_window=True)
    assert not client.deleted


def test_operator_must_acknowledge_paused_writers_and_backup_path_must_be_new(setup_tool, tmp_path):
    tool, _, client = setup_tool
    report = tool.scan()
    with pytest.raises(MaintenanceError):
        tool.cleanup(report, tmp_path / "backup", apply=True)
    backup = tmp_path / "existing"
    backup.mkdir()
    with pytest.raises(FileExistsError):
        tool.cleanup(report, backup, apply=True, maintenance_window=True)
    assert not client.deleted


def test_delete_failure_keeps_full_backup_and_interrupted_receipt(setup_tool, tmp_path):
    tool, _, client = setup_tool
    def fail(): raise RuntimeError("injected outage")
    client.on_delete = fail
    backup = tmp_path / "backup"
    with pytest.raises(RuntimeError):
        tool.cleanup(tool.scan(), backup, apply=True, maintenance_window=True)
    assert len(json.loads((backup / "vectors.json").read_text())) == 2
    assert json.loads((backup / "cleanup-result.json").read_text())["status"] == "interrupted"
    assert not client.deleted


def test_incomplete_scan_closes_iterator_and_never_produces_deletion_manifest(setup_tool):
    tool, _, client = setup_tool
    with pytest.raises(MaintenanceError):
        tool.scan(max_rows=1)
    assert client.closed and not client.deleted


def test_empty_manifest_never_runs_an_unfiltered_query(setup_tool):
    tool, _, client = setup_tool
    client.rows.clear()
    report = tool.scan()
    assert tool.cleanup(report)["planned"] == 0 and not client.deleted


def test_tampered_document_number_cannot_hide_an_existing_document(setup_tool, tmp_path):
    tool, engine, client = setup_tool
    report = tool.scan()
    document(engine, 7)
    report["orphans"][0]["page_id"] = 999
    with pytest.raises(MaintenanceError):
        tool.cleanup(report, tmp_path / "backup", apply=True, maintenance_window=True)
    assert not client.deleted


def test_vector_changed_after_backup_is_preserved(setup_tool, tmp_path):
    tool, _, client = setup_tool
    backup = tmp_path / "backup"
    report = tool.scan()
    def change_after_backup():
        if (backup / "manifest.json").exists():
            client.rows["7_0"]["embedding"] = [.2, .3]
    client.on_query = change_after_backup
    with pytest.raises(MaintenanceError):
        tool.cleanup(report, backup, apply=True, maintenance_window=True)
    assert not client.deleted and client.rows["7_0"]["embedding"] == [.2, .3]
    assert json.loads((backup / "cleanup-result.json").read_text())["status"] == "interrupted"
