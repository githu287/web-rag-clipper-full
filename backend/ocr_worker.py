"""One-shot worker. Reads bounded bytes on stdin, emits bounded JSON, no files."""
from __future__ import annotations

import io
import json
import sys
import warnings

MAX_PIXELS = 20_000_000
MAX_TEXT_CHARS = 20000
TILE_HEIGHT = 1536
TILE_OVERLAP = 160


def recognize(content: bytes, engine_factory=None) -> dict:
    from PIL import Image, ImageOps, UnidentifiedImageError
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as source:
                if source.format not in {"PNG", "JPEG", "WEBP", "GIF", "BMP"}:
                    return {"error": "仅支持 PNG、JPEG、WebP、GIF 和 BMP 图片"}
                width, height = source.size
                if width * height > MAX_PIXELS or max(width, height) > 40000:
                    return {"error": "图片超过 2000 万像素或边长 40000 限制"}
                animated = getattr(source, "n_frames", 1) > 1
                image = ImageOps.exif_transpose(source).convert("RGBA")
                background = Image.new("RGBA", image.size, "white")
                background.alpha_composite(image)
                image = background.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return {"error": "图片损坏、格式不支持或尺寸过大"}
    notices = ["animated_first_frame"] if animated else []
    if image.width > 1600:
        image = image.resize((1600, max(1, round(image.height * 1600 / image.width))))
        notices.append("resized")
    if engine_factory is None:
        from rapidocr_onnxruntime import RapidOCR
        engine_factory = lambda: RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1,
                                           det_limit_side_len=1536, det_limit_type="max")
    import numpy as np
    engine = engine_factory()
    lines = []
    tiles = 0
    low_confidence = 0
    text_chars = 0
    truncated = False
    stride = TILE_HEIGHT - TILE_OVERLAP
    for top in range(0, image.height, stride):
        bottom = min(image.height, top + TILE_HEIGHT)
        tile = image.crop((0, top, image.width, bottom))
        rows, _ = engine(np.asarray(tile)[:, :, ::-1].copy())  # RapidOCR ndarray input is BGR.
        tiles += 1
        for box, text, score in rows or []:
            # Overlap ownership uses box center, not text deduplication: repeated
            # legitimate lines survive and seam detections are included once.
            center = sum(float(point[1]) for point in box) / 4
            if top and center < TILE_OVERLAP / 2:
                continue
            if bottom < image.height and center >= TILE_HEIGHT - TILE_OVERLAP / 2:
                continue
            text = str(text).strip()
            if not text or float(score) < 0.5:
                continue
            low_confidence += float(score) < 0.8
            lines.append(text)
            text_chars += len(text) + 1
            if text_chars > MAX_TEXT_CHARS:
                truncated = True
                break
        if truncated or bottom == image.height:
            break
    if truncated:
        notices.append("text_truncated")
    if low_confidence:
        notices.append("low_confidence")
    return {"text": "\n".join(lines)[:MAX_TEXT_CHARS], "width": width, "height": height,
            "tiles": tiles, "warnings": notices, "engine": "rapidocr-ppocrv4-local"}


def main():
    content = sys.stdin.buffer.read(6 * 1024 * 1024 + 1)
    result = {"error": "图片超过 6 MiB 限制"} if len(content) > 6 * 1024 * 1024 else recognize(content)
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main()
