"""Real offline CPU model smoke test. Install backend/requirements-ocr.txt first."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend.services.ocr import OcrService


async def main():
    service = OcrService()
    fixtures = ROOT / "extension/tests/fixtures/ocr"
    bilingual = await service.recognize((fixtures / "bilingual.png").read_bytes())
    compact = bilingual["text"].replace(" ", "")
    for expected in ("图片文字识别测试", "中文与English2026", "请核对识别结果后保存"):
        assert expected in compact, (expected, bilingual)
    long = await service.recognize((fixtures / "long.png").read_bytes())
    compact = long["text"].replace(" ", "")
    expected = ["长图开始2026", "重复正文", "分段边界文字", "边界之后正文", "重复正文", "第二边界文字", "长图结束2026"]
    cursor = 0
    for text in expected:
        index = compact.find(text, cursor)
        assert index >= cursor, (text, long)
        cursor = index + len(text)
    assert compact.count("重复正文") == 2, long
    assert compact.count("分段边界文字") == 1, long
    assert long["tiles"] == 3, long
    print("Real local OCR passed: Chinese/English, 3 long-image tiles, seams, order and legitimate repeated lines.")


if __name__ == "__main__":
    asyncio.run(main())
