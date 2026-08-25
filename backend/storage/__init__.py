"""
原始文件存储层（Phase 2.10 Step 2）。

提供：
    - FileStorage：存储抽象（Protocol），定义 save / delete 契约。
    - LocalFileStorage：本地磁盘实现，仅写入 upload_dir，禁止路径穿越。

分层边界：
    - 本包只负责「字节 → 磁盘文件」与「文件删除」，不解析内容、不切分文本；
    - 不连接 MinIO / S3 / 任何对象存储（本阶段不引入 boto3/minio SDK）；
    - 上层（Parser / 后续 Upload Service）通过 FileStorage Protocol 依赖注入，
      未来可无痛替换为对象存储实现。
"""

from __future__ import annotations

from .local import LocalFileStorage
from .protocol import FileStorage

__all__ = ["FileStorage", "LocalFileStorage"]
