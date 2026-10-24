"""Bounded LRU cache whose active clients remain alive until their users finish."""
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import logging
import threading
from typing import Generic, Protocol, TypeVar


class Closable(Protocol):
    def close(self) -> None: ...


T = TypeVar("T", bound=Closable)


@dataclass
class _Entry(Generic[T]):
    client: T
    users: int = 0
    retired: bool = False


class ClientCache(Generic[T]):
    def __init__(self, capacity: int, logger: logging.Logger) -> None:
        if capacity < 1:
            raise ValueError("client cache capacity must be positive")
        self._capacity = capacity
        self._logger = logger
        self._entries: OrderedDict[str, _Entry[T]] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    @contextmanager
    def lease(self, key: str, factory: Callable[[], T]) -> Iterator[T]:
        evicted = None
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                # Construction is local and lazy. The actual network request
                # happens outside this lock, inside the caller's with block.
                entry = _Entry(factory())
                self._entries[key] = entry
            self._entries.move_to_end(key)
            entry.users += 1
            if len(self._entries) > self._capacity:
                _, oldest = self._entries.popitem(last=False)
                oldest.retired = True
                if oldest.users == 0:
                    evicted = oldest.client
        if evicted is not None:
            self._close(evicted)
        try:
            yield entry.client
        finally:
            with self._lock:
                entry.users -= 1
                close = entry.retired and entry.users == 0
            if close:
                self._close(entry.client)

    def clear(self) -> None:
        """Drain cached clients; active leases close on release. Safe to reuse."""
        idle = []
        with self._lock:
            for entry in self._entries.values():
                entry.retired = True
                if entry.users == 0:
                    idle.append(entry.client)
            self._entries.clear()
        for client in idle:
            self._close(client)

    def _close(self, client: T) -> None:
        try:
            client.close()
        except Exception as exc:
            # Cleanup must not replace a request result or its original error.
            self._logger.warning("Model client cleanup failed: %s", exc)
