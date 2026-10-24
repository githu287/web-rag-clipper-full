"""Real concurrent requests, eviction and shutdown must not invalidate users."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.clients.client_cache import ClientCache
from backend.clients.embedding import EmbeddingClient
from backend.clients.llm import BailianLLMClient
from backend.core.config import Settings
from backend.core import di
from backend.main import lifespan
from backend.workers import ingest_worker


def test_simultaneous_cache_misses_construct_one_client():
    cache = ClientCache(1, logging.getLogger(__name__))
    constructing, release = threading.Event(), threading.Event()
    gate = threading.Barrier(3)
    client = Mock()
    def factory():
        constructing.set()
        assert release.wait(3)
        return client
    create = Mock(side_effect=factory)
    def request():
        gate.wait(timeout=3)
        with cache.lease("same-key", create) as borrowed:
            return borrowed
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(request) for _ in range(2)]
        gate.wait(timeout=3)
        try:
            assert constructing.wait(3)
        finally:
            release.set()
        assert all(f.result(timeout=3) is client for f in futures)
    create.assert_called_once()
    cache.clear()
    client.close.assert_called_once()


@pytest.mark.parametrize("kind", ["embedding", "llm"])
@pytest.mark.parametrize("action", ["evict", "shutdown"])
def test_active_sdk_request_survives_eviction_or_shutdown(monkeypatch, kind, action):
    module = __import__(f"backend.clients.{kind}", fromlist=["OpenAI"])
    started, release = threading.Event(), threading.Event()
    created = []
    class FakeClient:
        def __init__(self, **kwargs):
            self.key, self.closed = kwargs["api_key"], False
            self.embeddings = SimpleNamespace(create=self.request)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.request))
            created.append(self)
        def request(self, **kwargs):
            if self.key == "slow":
                started.set()
                assert release.wait(3)
            assert not self.closed, "connection was closed while request was active"
            return SimpleNamespace(
                data=[SimpleNamespace(index=0, embedding=[0.1] * 1024)],
                choices=[SimpleNamespace(message=SimpleNamespace(content="answer"), finish_reason="stop")],
            )
        def close(self):
            assert not self.closed, "client closed more than once"
            self.closed = True
    monkeypatch.setattr(module, "OpenAI", FakeClient)
    monkeypatch.setattr(module, "_MAX_CLIENT_CACHE_SIZE", 1)
    service = (EmbeddingClient if kind == "embedding" else BailianLLMClient)(Settings(_env_file=None))
    def request(key):
        if kind == "embedding":
            return service.embed(["body"], api_key=key)
        return service.generate("system", "question", api_key=key)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(request, "slow")
        try:
            assert started.wait(3)
            if action == "evict":
                request("fast")
            else:
                service.close()
            assert not created[0].closed
        finally:
            release.set()
        assert future.result(timeout=3)
    assert created[0].closed
    service.close()
    assert all(client.closed for client in created)


def test_evicted_client_waits_for_all_overlapping_leases():
    cache = ClientCache(1, logging.getLogger(__name__))
    first, second = Mock(), Mock()
    with cache.lease("first", lambda: first):
        with cache.lease("first", lambda: pytest.fail("unexpected duplicate")):
            with cache.lease("second", lambda: second):
                first.close.assert_not_called()
        first.close.assert_not_called()
    first.close.assert_called_once()
    cache.clear()
    cache.clear()
    second.close.assert_called_once()


def test_failed_request_keeps_original_error_when_cleanup_fails(caplog):
    cache = ClientCache(1, logging.getLogger(__name__))
    client = Mock(close=Mock(side_effect=RuntimeError("close failed")))
    with pytest.raises(ValueError, match="request failed"):
        with cache.lease("first", lambda: client):
            cache.clear()
            raise ValueError("request failed")
    client.close.assert_called_once()
    assert "close failed" in caplog.text


def test_failed_constructor_does_not_poison_cache():
    cache = ClientCache(1, logging.getLogger(__name__))
    with pytest.raises(ValueError, match="construction"):
        with cache.lease("key", Mock(side_effect=ValueError("construction"))):
            pytest.fail("constructor should fail")
    client = Mock()
    with cache.lease("key", lambda: client) as borrowed:
        assert borrowed is client
    cache.clear()
    client.close.assert_called_once()


def test_cache_reuse_after_cleanup_does_not_close_new_same_key_client():
    cache = ClientCache(1, logging.getLogger(__name__))
    original, replacement = Mock(), Mock()
    with cache.lease("same-key", lambda: original):
        cache.clear()
        with cache.lease("same-key", lambda: replacement):
            original.close.assert_not_called()
        replacement.close.assert_not_called()
    original.close.assert_called_once()
    replacement.close.assert_not_called()
    cache.clear()
    replacement.close.assert_called_once()


def test_cleanup_does_not_initialize_unused_dependencies(monkeypatch):
    factories = [Mock(), Mock()]
    for name, factory in zip(["get_embedding_client", "get_llm_client"], factories):
        factory.cache_info.return_value = SimpleNamespace(currsize=0)
        monkeypatch.setattr(di, name, factory)
    di.close_model_clients()
    for factory in factories:
        factory.assert_not_called()


def test_cleanup_drains_both_initialized_clients_even_if_one_fails(monkeypatch):
    factories = [Mock(), Mock()]
    for name, factory in zip(["get_embedding_client", "get_llm_client"], factories):
        factory.cache_info.return_value = SimpleNamespace(currsize=1)
        monkeypatch.setattr(di, name, factory)
    factories[0].return_value.close.side_effect = RuntimeError("cleanup failed")
    di.close_model_clients()
    for factory in factories:
        factory.return_value.close.assert_called_once()


@pytest.mark.parametrize("fail_startup", [False, True])
def test_api_lifespan_always_drains_model_clients(monkeypatch, fail_startup):
    initializer, cleanup = Mock(), Mock()
    if fail_startup:
        initializer.initialize.side_effect = RuntimeError("startup failed")
    monkeypatch.setattr("backend.main.get_milvus_initializer", lambda: initializer)
    monkeypatch.setattr("backend.main.close_model_clients", cleanup)
    async def scenario():
        async with lifespan(Mock()):
            pass
    if fail_startup:
        with pytest.raises(RuntimeError, match="startup failed"):
            asyncio.run(scenario())
    else:
        asyncio.run(scenario())
    cleanup.assert_called_once()


@pytest.mark.parametrize("fail_startup", [False, True])
def test_worker_shutdown_and_startup_failure_drain_model_clients(monkeypatch, fail_startup):
    worker, cleanup, initializer = Mock(), Mock(), Mock()
    if fail_startup:
        initializer.initialize.side_effect = RuntimeError("startup failed")
    monkeypatch.setattr(ingest_worker, "get_milvus_initializer", lambda: initializer)
    monkeypatch.setattr(ingest_worker, "IngestWorker", lambda: worker)
    monkeypatch.setattr(ingest_worker, "close_model_clients", cleanup)
    stop = threading.Event()
    stop.set()
    if fail_startup:
        with pytest.raises(RuntimeError, match="startup failed"):
            ingest_worker.main(stop)
    else:
        ingest_worker.main(stop)
    cleanup.assert_called_once()
