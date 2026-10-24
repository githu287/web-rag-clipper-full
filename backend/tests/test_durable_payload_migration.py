"""Exercise 0011 on SQLite and generate its MySQL DDL without live services."""

import importlib.util
from io import StringIO
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from backend.models.base import Base
from backend.repositories.mysql import IngestJobRepositoryImpl, PluginRepositoryImpl


def migration():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/0011_durable_ingest_payload.py"
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
                columns = {col["name"] for col in inspect(connection).get_columns("ingest_jobs")}
                assert "payload" not in columns
                assert "payload_expires_at" not in columns
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
        migration().upgrade()
    sql = output.getvalue()
    assert "ADD COLUMN payload JSON" in sql
    assert "ADD COLUMN payload_expires_at DATETIME" in sql
    assert "(status, created_at, id)" in sql
    assert "(payload_expires_at)" in sql
