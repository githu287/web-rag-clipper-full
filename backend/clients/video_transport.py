"""Synchronous HTTP transport whose socket operations share one deadline."""
from contextlib import contextmanager
import socket

import httpcore
import httpx

from ..core.video_errors import VideoError


@contextmanager
def _operation_timeout(remaining, timeout):
    budget = remaining()
    bounded_by_deadline = timeout is None or budget <= timeout
    try:
        yield budget if bounded_by_deadline else timeout
    except (httpcore.TimeoutException, socket.timeout):
        remaining()  # Preserve a cancellation detected while the socket waited.
        if bounded_by_deadline:
            # Socket timers may fire a fraction before monotonic reaches the
            # deadline. The exhausted budget, not clock rounding, determines 504.
            raise VideoError("云端请求超时，请稍后手动重试", 504) from None
        raise


@contextmanager
def _httpx_errors():
    # Both response headers and streamed bodies must reach the SDK as HTTPX
    # errors. Keep provider error text out of this application's response.
    try:
        yield
    except httpcore.TimeoutException as exc:
        raise httpx.TimeoutException(str(exc)) from exc
    except httpcore.NetworkError as exc:
        raise httpx.NetworkError(str(exc)) from exc
    except httpcore.ProtocolError as exc:
        raise httpx.ProtocolError(str(exc)) from exc
    except httpcore.UnsupportedProtocol as exc:
        raise httpx.UnsupportedProtocol(str(exc)) from exc


class _DeadlineNetworkStream(httpcore.NetworkStream):
    def __init__(self, stream, remaining):
        self.stream, self.remaining = stream, remaining

    def read(self, max_bytes, timeout=None):
        with _operation_timeout(self.remaining, timeout) as bounded:
            return self.stream.read(max_bytes, timeout=bounded)

    def write(self, buffer, timeout=None):
        if not buffer:
            return
        # SyncStream.write loops over partial sends with one fixed timeout.
        # This backend opens a direct TCP connection and upgrades it at most
        # once; its public socket accessor returns the actual socket/SSLSocket,
        # whose send retains TLS encryption. No proxy or TLS-in-TLS is involved.
        connection = self.stream.get_extra_info("socket")
        if connection is None:
            raise httpcore.WriteError("Cloud transport did not expose its socket")
        try:
            view = memoryview(buffer)
            while view:
                with _operation_timeout(self.remaining, timeout) as bounded:
                    connection.settimeout(bounded)
                    sent = connection.send(view)
                if sent <= 0:
                    raise httpcore.WriteError("Cloud connection closed during upload")
                view = view[sent:]
        except socket.timeout as exc:
            raise httpcore.WriteTimeout(str(exc)) from exc
        except OSError as exc:
            raise httpcore.WriteError(str(exc)) from exc

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        with _operation_timeout(self.remaining, timeout) as bounded:
            stream = self.stream.start_tls(ssl_context, server_hostname=server_hostname, timeout=bounded)
        return _DeadlineNetworkStream(stream, self.remaining)

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)

    def close(self):
        self.stream.close()


class _DeadlineBackend(httpcore.NetworkBackend):
    def __init__(self, remaining):
        self.backend, self.remaining = httpcore.SyncBackend(), remaining

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        with _operation_timeout(self.remaining, timeout) as bounded:
            stream = self.backend.connect_tcp(host, port, timeout=bounded,
                                              local_address=local_address, socket_options=socket_options)
        return _DeadlineNetworkStream(stream, self.remaining)


class _ResponseStream(httpx.SyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    def __iter__(self):
        with _httpx_errors():
            yield from self.stream

    def close(self):
        with _httpx_errors():
            self.stream.close()


class DeadlineTransport(httpx.BaseTransport):
    """Use public HTTPX/HTTPCore adapters; do not mutate SDK/private sockets.

    HTTPX's ordinary read timeout restarts for each socket read. Capping only
    the initial request timeout would still allow a late heartbeat to extend a
    video question beyond its remaining budget.
    """
    def __init__(self, remaining):
        self.pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            max_connections=1, max_keepalive_connections=0, retries=0,
            network_backend=_DeadlineBackend(remaining),
        )

    def handle_request(self, request):
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                             port=request.url.port, target=request.url.raw_path),
            headers=request.headers.raw, content=request.stream, extensions=request.extensions,
        )
        with _httpx_errors():
            response = self.pool.handle_request(core_request)
        return httpx.Response(response.status, headers=response.headers,
                              stream=_ResponseStream(response.stream), extensions=response.extensions)

    def close(self):
        self.pool.close()
