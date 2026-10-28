"""OCR auth, bounded reads, failure contract, CPU lifetime and tiled decoding."""
import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.api.deps import get_current_plugin
from backend.api.routers.ocr import router
from backend.core.di import get_ocr_service, get_plugin_service
from backend.core.exceptions import PluginCredentialsMissingError
from backend.services.ocr import MAX_IMAGE_BYTES, OcrError, OcrService


def client(service, authenticated=True):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_ocr_service] = lambda: service
    if authenticated:
        app.dependency_overrides[get_current_plugin] = lambda: SimpleNamespace(plugin_id="ocr-fixture")
    else:
        app.dependency_overrides[get_plugin_service] = lambda: Mock()
    return TestClient(app)


def test_auth_required_before_recognition():
    service = SimpleNamespace(recognize=AsyncMock())
    with client(service, False) as http:
        with pytest.raises(PluginCredentialsMissingError):
            http.post("/ocr/image", content=b"image", headers={"Content-Type": "image/png"})
    service.recognize.assert_not_awaited()


@pytest.mark.parametrize("status,code", [(415,"OCR_INVALID_IMAGE"), (429,"OCR_BUSY"), (503,"OCR_UNAVAILABLE"), (504,"OCR_TIMEOUT")])
def test_failure_contract(status, code):
    service = SimpleNamespace(recognize=AsyncMock(side_effect=OcrError(status, code, "识别失败")))
    with client(service) as http:
        response = http.post("/ocr/image", content=b"image", headers={"Content-Type": "image/png"})
    assert response.status_code == status
    assert response.json() == {"detail": {"code": code, "message": "识别失败"}}


def test_limits_and_mime_and_no_url_fetch():
    service = SimpleNamespace(recognize=AsyncMock())
    with client(service) as http:
        assert http.post("/ocr/image", json={"url": "http://internal/image"}).status_code == 415
        assert http.post("/ocr/image", content=b"x" * (MAX_IMAGE_BYTES + 1), headers={"Content-Type": "image/png"}).status_code == 413
    service.recognize.assert_not_awaited()


def test_result_does_not_require_model_key_or_write_document():
    result = dict(text="图片正文", width=900, height=300, tiles=1, warnings=[], engine="fixture")
    service = SimpleNamespace(recognize=AsyncMock(return_value=result))
    with client(service) as http:
        response = http.post("/ocr/image", content=b"pixels", headers={"Content-Type": "image/png"})
    assert response.status_code == 200
    assert response.json() == result
    service.recognize.assert_awaited_once_with(b"pixels")


def test_service_missing_optional_dependency():
    async def run():
        with patch("backend.services.ocr.importlib.util.find_spec", return_value=None):
            with pytest.raises(OcrError, match="未安装"):
                await OcrService().recognize(b"bytes")
    asyncio.run(run())


def test_timeout_kills_child_and_releases_capacity():
    async def run():
        process = SimpleNamespace(returncode=None, communicate=AsyncMock(side_effect=TimeoutError),
                                  kill=Mock(), wait=AsyncMock())
        service = OcrService()
        with patch("backend.services.ocr.importlib.util.find_spec", return_value=object()), patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            with pytest.raises(OcrError) as error:
                await service.recognize(b"bytes")
            assert error.value.status == 504
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()
        assert not service._lock.locked()
    asyncio.run(run())


def test_cancel_kills_child_and_rejects_concurrent_work():
    async def run():
        started = asyncio.Event()
        async def communicate(_content):
            started.set()
            await asyncio.Event().wait()
        process = SimpleNamespace(returncode=None, communicate=communicate, kill=Mock(), wait=AsyncMock())
        service = OcrService()
        with patch("backend.services.ocr.importlib.util.find_spec", return_value=object()), patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            task = asyncio.create_task(service.recognize(b"bytes"))
            await started.wait()
            with pytest.raises(OcrError) as error:
                await service.recognize(b"other")
            assert error.value.status == 429
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()
        assert not service._lock.locked()
    asyncio.run(run())


def test_http_disconnect_cancels_ongoing_recognition():
    from fastapi import HTTPException
    from backend.api.routers.ocr import recognize_image
    async def run():
        ended = asyncio.Event()
        async def recognize(_content):
            try:
                await asyncio.Event().wait()
            finally:
                ended.set()
        async def stream():
            yield b"image"
        request = SimpleNamespace(headers={"content-type": "image/png"}, stream=stream,
                                  is_disconnected=AsyncMock(return_value=True))
        with pytest.raises(HTTPException) as error:
            await recognize_image(request, SimpleNamespace(plugin_id="fixture"), SimpleNamespace(recognize=recognize))
        assert error.value.status_code == 499
        assert ended.is_set()
    asyncio.run(run())


def image_bytes(size=(200, 200), mode="RGB", format="PNG"):
    Image = pytest.importorskip("PIL.Image")
    image = Image.new(mode, size, "white")
    data = io.BytesIO()
    image.save(data, format=format)
    return data.getvalue()


def test_invalid_and_pixel_bomb_rejected_before_engine():
    pytest.importorskip("PIL")
    from backend.ocr_worker import recognize
    factory = Mock(side_effect=AssertionError("must not load engine"))
    assert "error" in recognize(b"<svg>not an image</svg>", factory)
    assert "error" in recognize(image_bytes((5000, 4001)), factory)
    factory.assert_not_called()


def test_tile_overlap_ownership_preserves_legitimate_repeated_lines():
    pytest.importorskip("numpy")
    from backend.ocr_worker import recognize
    def box(y): return [[0, y], [100, y], [100, y + 20], [0, y + 20]]
    engine = Mock(side_effect=[
        ([[box(20), "重复文字", .99], [box(1500), "交界文字", .99]], None),
        ([[box(124), "交界文字", .99], [box(400), "重复文字", .99]], None),
    ])
    result = recognize(image_bytes((600, 2000)), lambda: engine)
    assert result["text"] == "重复文字\n交界文字\n重复文字"
    assert result["tiles"] == 2


def test_low_confidence_and_wide_image_notice():
    pytest.importorskip("numpy")
    from backend.ocr_worker import recognize
    box = [[0, 20], [100, 20], [100, 40], [0, 40]]
    result = recognize(image_bytes((2000, 200)), lambda: lambda _image: ([[box, "需核对", .6], [box, "排除", .4]], None))
    assert result["text"] == "需核对"
    assert set(result["warnings"]) == {"resized", "low_confidence"}
