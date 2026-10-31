"""Exercise real loopback sockets, including periods with no SSE data at all."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import ssl
import threading
import time

import httpx
import httpcore
import pytest

from backend.clients.video import CloudVideoClient
from backend.core.video_errors import VideoError
from backend.clients.video_transport import _DeadlineNetworkStream


@contextmanager
def provider(mode):
    requests = []
    connected = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            connected.set()
            try:
                if mode == "headers":
                    time.sleep(1.4)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if mode == "late_heartbeat":
                    time.sleep(0.25)
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                if mode in ("body", "late_heartbeat"):
                    time.sleep(1.4)
                event = {"id": "fixture", "object": "chat.completion.chunk", "created": 1, "model": "fixture",
                         "choices": [{"index": 0, "delta": {"content": "complete"}, "finish_reason": "stop"}]}
                self.wfile.write(("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        yield {"api_key": "fixture-only", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
               "model": "fixture"}, requests, connected
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("mode", ["headers", "body", "late_heartbeat"])
def test_deadline_stops_silent_socket_without_waiting_for_another_sse_block(mode):
    with provider(mode) as (config, requests, _):
        started = time.monotonic()
        with pytest.raises(VideoError) as caught:
            CloudVideoClient().generate(config, [], lambda: True, timeout_seconds=0.4)
        elapsed = time.monotonic() - started
    assert caught.value.status == 504
    assert elapsed < 1.0, f"0.4-second budget took {elapsed:.3f} seconds"
    assert len(requests) == 1  # A timeout must never repeat a potentially paid request.


def test_cancellation_during_silent_socket_preserves_cancelled_status():
    with provider("body") as (config, requests, connected):
        with pytest.raises(VideoError) as caught:
            # Exercise cancellation after connection, independently of cold SDK
            # startup on a busy Windows runner. Deadline tests retain 0.4 seconds.
            CloudVideoClient().generate(config, [], lambda: not connected.is_set(), timeout_seconds=5)
    assert caught.value.status == 409 and len(requests) == 1


def test_default_deadline_transport_accepts_complete_sse_response():
    with provider("success") as (config, requests, _):
        text, usage = CloudVideoClient().generate(config, [{"type": "text", "text": "fixture"}], lambda: True,
                                                  timeout_seconds=2)
    assert text == "complete" and usage == {} and len(requests) == 1
    assert requests[0]["messages"][0]["content"][0]["text"] == "fixture"


def test_sdk_stream_network_error_is_sanitized_without_retry():
    requests = []

    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b": header accepted\n\n"
            raise httpx.ReadError("private-provider-response-and-signed-url")

    def handle(request):
        requests.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=BrokenStream())

    transport = httpx.Client(transport=httpx.MockTransport(handle))
    client = CloudVideoClient(transport_factory=lambda **_: transport)
    with pytest.raises(VideoError) as caught:
        client.generate({"api_key": "fixture", "base_url": "https://example.invalid/v1", "model": "fixture"}, [], lambda: True)
    assert caught.value.status == 502 and "private" not in str(caught.value)
    assert len(requests) == 1


@pytest.mark.parametrize("mode", ["headers", "body", "late_heartbeat"])
def test_asr_default_transport_shares_deadline_for_silent_or_late_body(tmp_path, monkeypatch, mode):
    from backend.clients.video_asr import CloudAsrClient
    audio = tmp_path / "fixture.wav"
    audio.write_bytes(b"private-fixture-audio")
    real_client = httpx.Client
    monkeypatch.setattr("backend.clients.video_asr.ASR_TIMEOUT_SECONDS", 0.4)
    with provider(mode) as (local_config, requests, _):
        class Loopback(httpx.BaseTransport):
            def __init__(self, delegate):
                self.delegate = delegate
            def handle_request(self, request):
                request.url = httpx.URL(local_config["base_url"])
                return self.delegate.handle_request(request)
            def close(self):
                self.delegate.close()
        def client(**options):
            return real_client(**{**options, "transport": Loopback(options["transport"])})
        monkeypatch.setattr("backend.clients.video_asr.httpx.Client", client)
        started = time.monotonic()
        with pytest.raises(VideoError) as caught:
            CloudAsrClient().transcribe(audio, 2, {"api_key": "fixture-only", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"})
        elapsed = time.monotonic() - started
    assert caught.value.status == 504 and elapsed < 1.0
    assert len(requests) == 1


def test_partial_upload_sends_recompute_remaining_budget_and_stop_without_repeating_bytes():
    state = {"now": 0, "sent": bytearray(), "timeouts": []}

    class Socket:
        def settimeout(self, value):
            state["timeouts"].append(value)

        def send(self, data):
            state["now"] += .09
            state["sent"].extend(data[:1])
            return 1

    class Stream:
        def get_extra_info(self, key):
            assert key == "socket"
            return Socket()

    def remaining():
        if state["now"] >= .2:
            raise VideoError("deadline", 504)
        return .2 - state["now"]

    with pytest.raises(VideoError) as caught:
        _DeadlineNetworkStream(Stream(), remaining).write(b"abcdef", timeout=30)
    assert caught.value.status == 504 and state["sent"] == b"abc"
    assert state["timeouts"] == pytest.approx([.2, .11, .02])


@pytest.mark.parametrize("error, expected", [(socket.timeout("private"), httpcore.WriteTimeout),
                                           (OSError("private"), httpcore.WriteError)])
def test_socket_write_errors_use_httpcore_types_without_closing_owned_stream(error, expected):
    from unittest.mock import Mock
    connection = Mock()
    connection.send.side_effect = error
    stream = Mock()
    stream.get_extra_info.return_value = connection
    wrapped = _DeadlineNetworkStream(stream, lambda: 50)
    with pytest.raises(expected):
        wrapped.write(b"data", timeout=30)
    connection.settimeout.assert_called_once_with(30)
    stream.close.assert_not_called()
    wrapped.close()
    stream.close.assert_called_once()


def test_empty_write_does_not_touch_network_or_deadline_and_zero_send_terminates():
    from unittest.mock import Mock
    stream, remaining = Mock(), Mock(return_value=1)
    wrapped = _DeadlineNetworkStream(stream, remaining)
    wrapped.write(b"")
    stream.get_extra_info.assert_not_called()
    remaining.assert_not_called()
    stream.get_extra_info.return_value.send.return_value = 0
    with pytest.raises(httpcore.WriteError):
        wrapped.write(b"data")
    stream.get_extra_info.return_value.send.assert_called_once()


def test_direct_tls_upload_uses_the_ssl_socket_and_delivers_plaintext_only_to_tls_peer(tmp_path):
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
                   .not_valid_after(now + timedelta(days=1)).add_extension(
                       x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
                   .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "loopback.pem", tmp_path / "loopback-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    client_context = ssl.create_default_context(cafile=str(cert_path))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(3)
    received, failures = bytearray(), []
    payload = b"TLS upload fixture" * 4096

    def receive():
        try:
            connection, _ = listener.accept()
            with server_context.wrap_socket(connection, server_side=True) as tls:
                tls.settimeout(3)
                while len(received) < len(payload):
                    block = tls.recv(8192)
                    if not block:
                        break
                    received.extend(block)
                tls.sendall(b"received")
        except Exception as exc:
            failures.append(type(exc).__name__)

    thread = threading.Thread(target=receive, daemon=True)
    thread.start()
    stream = None
    try:
        stream = _DeadlineNetworkStream(httpcore.SyncBackend().connect_tcp("127.0.0.1", listener.getsockname()[1], timeout=3), lambda: 3)
        stream = stream.start_tls(client_context, server_hostname="localhost", timeout=3)
        assert isinstance(stream.get_extra_info("socket"), ssl.SSLSocket)
        stream.write(payload, timeout=3)
        assert stream.read(8, timeout=3) == b"received"
    finally:
        if stream is not None:
            stream.close()
        thread.join(timeout=4)
        listener.close()
    assert not thread.is_alive() and not failures and received == payload


@pytest.mark.parametrize("operation", ["read", "write", "start_tls"])
def test_deadline_limited_socket_timeout_is_504_even_if_platform_timer_fires_early(operation):
    from unittest.mock import Mock
    stream = Mock()
    stream.read.side_effect = httpcore.ReadTimeout("fixture")
    stream.start_tls.side_effect = httpcore.ConnectTimeout("fixture")
    stream.get_extra_info.return_value.send.side_effect = socket.timeout("fixture")
    wrapped = _DeadlineNetworkStream(stream, lambda: .005)
    with pytest.raises(VideoError) as caught:
        if operation == "read":
            wrapped.read(1024, timeout=30)
        elif operation == "write":
            wrapped.write(b"data", timeout=30)
        else:
            wrapped.start_tls(None, timeout=30)
    assert caught.value.status == 504


def test_cancellation_takes_priority_over_deadline_limited_socket_timeout():
    from unittest.mock import Mock
    stream = Mock()
    stream.read.side_effect = httpcore.ReadTimeout("fixture")
    remaining = Mock(side_effect=[.005, VideoError("cancelled", 409)])
    with pytest.raises(VideoError) as caught:
        _DeadlineNetworkStream(stream, remaining).read(1024, timeout=30)
    assert caught.value.status == 409


@pytest.mark.parametrize("cancel_during_request", [False, True])
def test_real_sdk_preserves_preheader_deadline_classification_when_timer_fires_early(monkeypatch, cancel_during_request):
    from backend.clients.video_transport import _operation_timeout
    # Keep the application's wall-clock check before its deadline. This is the
    # platform-timer edge case, not an already expired budget caught by check().
    monkeypatch.setattr("backend.clients.video.time.monotonic", lambda: 0)
    state = {"calls": 0, "alive": True}

    def handle(request):
        state["calls"] += 1
        state["alive"] = not cancel_during_request
        # The actual SDK catches this transport exception before headers and
        # wraps it in APIConnectionError with VideoError as its direct cause.
        with _operation_timeout(lambda: .005, 30):
            raise httpcore.ReadTimeout("private-provider-detail")

    transport = httpx.Client(transport=httpx.MockTransport(handle))
    client = CloudVideoClient(transport_factory=lambda **_: transport)
    with pytest.raises(VideoError) as caught:
        client.generate({"api_key": "fixture-only", "base_url": "https://example.invalid/v1", "model": "fixture"},
                        [], lambda: state["alive"], timeout_seconds=.4)
    assert caught.value.status == (409 if cancel_during_request else 504)
    assert state["calls"] == 1 and "private" not in str(caught.value)
