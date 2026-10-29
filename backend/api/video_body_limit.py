"""Bound multipart traffic before FastAPI/Starlette spools the request to disk."""
from starlette.formparsers import MultiPartException
from starlette.responses import JSONResponse


class VideoBodyLimit:
    def __init__(self, app, max_bytes):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].rstrip("/") != "/videos/upload":
            return await self.app(scope, receive, send)
        # Include multipart boundaries and filename headers, but not a second whole video.
        limit = self.max_bytes + 1024 * 1024
        headers = dict(scope.get("headers", []))
        try:
            if int(headers.get(b"content-length", b"0")) > limit:
                return await JSONResponse(status_code=413, content={"detail": "视频文件超过大小限制"})(scope, receive, send)
        except ValueError:
            return await JSONResponse(status_code=400, content={"detail": "Content-Length 不正确"})(scope, receive, send)
        count, too_large = 0, False

        async def bounded_receive():
            nonlocal count, too_large
            message = await receive()
            count += len(message.get("body", b""))
            if count > limit:
                too_large = True
                # Starlette catches this type and closes all partially spooled upload files.
                raise MultiPartException("视频文件超过大小限制")
            return message

        async def bounded_send(message):
            if too_large and message["type"] == "http.response.start":
                message = {**message, "status": 413}
            await send(message)

        return await self.app(scope, bounded_receive, bounded_send)
