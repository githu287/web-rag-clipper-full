"""Exercise both 0011 variants upgrading to 0012 without live services."""

import importlib.util
from io import StringIO
from pathlib import Path
from datetime import datetime, timedelta

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, update

from backend.models.base import Base
from backend.models.ingest_job import IngestJob
from backend.repositories.mysql import IngestJobRepositoryImpl, PluginRepositoryImpl


def migration(revision="0012"):
    filename = "0012_ingest_payload_deadline.py" if revision == "0012" else "0011_durable_ingest_payload.py"
    path = Path(__file__).resolve().parents[2] / "alembic/versions" / filename
    spec = importlib.util.spec_from_file_location("durable_payload_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_downgrade_preserves_legacy_jobs():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    PluginRepositoryImpl(engine).create_plugin("a", "Test", "test", "a" * 64)
    job = IngestJobRepositoryImpl(engine).create_job("a", "WEB_CLIP")
    module = migration()
    try:
        with engine.begin() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                module.downgrade()
                migration("0011").downgrade()
                columns = {col["name"] for col in inspect(connection).get_columns("ingest_jobs")}
                assert "payload" not in columns
                assert "payload_expires_at" not in columns
                migration("0011").upgrade()
                module.upgrade()
                indexes = {index["name"] for index in inspect(connection).get_indexes("ingest_jobs")}
                assert {"ix_ingest_jobs_status_created", "ix_ingest_jobs_payload_expires"} <= indexes
        saved = IngestJobRepositoryImpl(engine).get_job(job.id, "a")
        assert saved.status == "QUEUED"
        assert saved.payload is saved.payload_expires_at is None
    finally:
        engine.dispose()


def test_mysql_upgrade_emits_json_and_recovery_indexes():
    output = StringIO()
    context = MigrationContext.configure(dialect_name="mysql", opts={"as_sql": True, "output_buffer": output})
    with Operations.context(context):
        migration("0011").upgrade()
        migration().upgrade()
    sql = output.getvalue()
    assert "ADD COLUMN payload JSON" in sql
    assert "ADD COLUMN payload_expires_at DATETIME" in sql
    assert "(status, created_at, id)" in sql
    assert "(payload_expires_at)" in sql


def test_published_schema_backfills_deadlines_and_local_variant_preserves_them():
    engine = create_engine("sqlite://")
    try:
        Base.metadata.create_all(engine)
        PluginRepositoryImpl(engine).create_plugin("a", "Test", "test", "a" * 64)
        jobs = IngestJobRepositoryImpl(engine)
        job = jobs.create_job("a", "WEB_CLIP", payload={"raw_text": "synthetic"})
        original = jobs.get_job(job.id, "a").payload_expires_at
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                # Earlier local 0011 already contains the added column/index.
                migration().upgrade()
        assert jobs.get_job(job.id, "a").payload_expires_at == original
        created = datetime(2026, 1, 1)
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration().downgrade()
                connection.execute(update(IngestJob.__table__).where(IngestJob.id == job.id).values(created_at=created))
                # Published GitHub 0011 has payload, but has no deadline yet.
                migration().upgrade()
        final = jobs.get_job(job.id, "a")
        assert final.payload_expires_at == created + timedelta(days=7)
        assert final.payload == {"raw_text": "synthetic"}
    finally:
        engine.dispose()
