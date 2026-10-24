"""Bounded reads of framework-spooled uploads, called from worker threads."""

from fastapi import UploadFile

from ..core.exceptions import DocumentFileTooLargeError

_READ_CHUNK_BYTES = 64 * 1024


def read_upload_content(upload: UploadFile, max_bytes: int) -> bytes:
    """Read at most limit + 1 bytes; metadata never replaces byte counting.

    Multipart parsing/spooling has already happened before the endpoint runs.
    This bounds the endpoint's copy, not the incoming HTTP request body.
    """
    if max_bytes < 1:
        raise ValueError("upload limit must be positive")
    if upload.size is not None and upload.size > max_bytes:
        raise DocumentFileTooLargeError(f"file size exceeds limit {max_bytes} bytes")

    content = bytearray()
    while True:
        part = upload.file.read(min(_READ_CHUNK_BYTES, max_bytes + 1 - len(content)))
        if not part:
            return bytes(content)
        content.extend(part)
        if len(content) > max_bytes:
            raise DocumentFileTooLargeError(f"file size exceeds limit {max_bytes} bytes")
