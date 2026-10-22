"""Waiting budgets track configuration without publishing credentials."""

from types import SimpleNamespace

from backend.core.di import get_settings
from backend.core.request_budget import request_timeouts_ms
from backend.main import create_app
from fastapi.testclient import TestClient


def settings(embedding=30, llm=60, retries=2):
    return SimpleNamespace(
        bailian_embedding_timeout_seconds=embedding,
        bailian_llm_timeout_seconds=llm,
        bailian_max_retries=retries,
        bailian_api_key="never-publish-this-key",
    )


def test_default_budgets_cover_both_models_and_retry_after():
    assert request_timeouts_ms(settings()) == {
        "search": 240000, "ask": 540000,
        "api_key": 240000, "model_config": 540000,
    }


def test_custom_timeouts_and_disabled_retries():
    assert request_timeouts_ms(settings(embedding=10, llm=100, retries=0)) == {
        "search": 40000, "ask": 140000,
        "api_key": 40000, "model_config": 140000,
    }


def test_browser_timer_limit_never_overflows():
    assert set(request_timeouts_ms(settings(10**9, 10**9, 100)).values()) == {2147483647}


def test_public_catalog_uses_injected_settings_without_credentials():
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings(45, 90, 1)
    # No lifespan: public policy must need neither database nor Milvus/network.
    response = TestClient(app).get("/plugins/model-providers")
    assert response.status_code == 200
    assert response.json()["request_timeouts_ms"] == request_timeouts_ms(settings(45, 90, 1))
    assert set(response.json()) == {"embedding", "llm", "request_timeouts_ms"}
    assert "never-publish-this-key" not in response.text
