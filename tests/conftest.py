"""Root-level test setup.

让仓库根目录下的 `tests/` 可直接 import `evaluation.*` / `backend.*`
（pytest 默认只把测试文件所在目录加入 sys.path，不含仓库根目录）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
