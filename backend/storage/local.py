"""
LocalFileStorage：本地磁盘文件存储实现（Phase 2.10 Step 2）。

安全红线（本文件核心）：
1) 不信任客户端提交的完整路径 / 绝对路径 / 含 `..` 的路径：
   - save：文件名必须为纯文件名（不含任何路径分隔符、非 `.`/`..`）；
     否则抛 DocumentStoragePathTraversalError。
   - delete / resolve：相对路径经解析后必须仍位于 upload_dir 内
     （commonpath 校验），越界抛 DocumentStoragePathTraversalError。
2) 最终只允许写入 upload_dir 内。
3) delete 对不存在文件保持幂等（FileNotFoundError 静默返回）。
4) 不连接 MinIO / S3，不引入 boto3/minio SDK。
5) save / resolve / delete 围绕同一逻辑路径语义工作：
   save → 相对 upload_dir 的逻辑路径；
   resolve → 逻辑路径转为绝对物理路径（Parser 可直接打开）；
   delete → 逻辑路径删除对应物理文件。
"""

from __future__ import annotations

import logging
import os

from ..core.exceptions import (
    DocumentStorageError,
    DocumentStoragePathTraversalError,
)

logger: logging.Logger = logging.getLogger(__name__)

# Windows 与 POSIX 的路径分隔符全集：无论运行平台，一律拒绝 `/` 与 `\`
# （防御：避免 POSIX 上接收 Windows 风格路径、Windows 上接收 POSIX 风格路径）
_PATH_SEPARATORS: tuple[str, ...] = ("/", "\\")


class LocalFileStorage:
    """本地磁盘存储：save/delete 均限定在 upload_dir 内。"""

    def __init__(self, upload_dir: str) -> None:
        """
        Args:
            upload_dir: 存储根目录（相对项目根或绝对路径）；首次 save 时自动创建。
        """
        if not upload_dir:
            raise ValueError("upload_dir 不能为空")
        self._upload_dir: str = upload_dir

    # ---------------------------------------------------------------- 对外 API
    def save(self, filename: str, data: bytes) -> str:
        """
        保存字节数据到 upload_dir 内，返回相对路径（可直接写 Document.file_path）。

        Args:
            filename: 客户端提交的原始文件名（必须为纯文件名，含路径片段将拦截）。
            data: 文件字节内容。

        Returns:
            相对 upload_dir 的相对路径（本实现等价于消毒后的文件名）。

        Raises:
            DocumentStoragePathTraversalError: 文件名含路径分隔符 / `..` / 空。
            DocumentStorageError: 目录创建或文件写入失败（保留根因）。
        """
        safe_name = self._validate_filename(filename)

        try:
            os.makedirs(self._upload_dir, exist_ok=True)
        except OSError as exc:
            raise DocumentStorageError(
                f"创建上传目录失败: {self._upload_dir}"
            ) from exc

        dest_path = self._resolve(safe_name)
        try:
            with open(dest_path, "wb") as fh:
                fh.write(data)
        except OSError as exc:
            raise DocumentStorageError(
                f"保存文件失败: {safe_name}"
            ) from exc

        logger.info(
            "LocalFileStorage.save: filename=%s → %s", safe_name, dest_path
        )
        # 返回相对路径（已通过 _validate_filename / _resolve 双重校验，安全）
        return safe_name

    def resolve(self, file_path: str) -> str:
        """
        将逻辑路径解析为 Parser 可直接打开的绝对物理路径。

        复用 _resolve() 的越界校验：解析后必须仍位于 upload_dir 内，
        越界抛 DocumentStoragePathTraversalError；与 save / delete 同一路径语义。

        Args:
            file_path: save 返回的相对路径（或 upload_dir 内相对路径）。

        Returns:
            绝对物理路径（可直接 open()，与 save 实际落盘位置一致）。
        """
        return self._resolve(file_path)

    def delete(self, file_path: str) -> None:
        """
        删除 upload_dir 内的文件；文件不存在时幂等返回。

        Args:
            file_path: save 返回的相对路径（或 upload_dir 内相对路径）。

        Raises:
            DocumentStoragePathTraversalError: 解析后越出 upload_dir。
            DocumentStorageError: 删除失败（权限 / IO 等）。
        """
        dest_path = self._resolve(file_path)
        try:
            os.remove(dest_path)
        except FileNotFoundError:
            # 幂等：目标不存在视为删除成功
            logger.debug("LocalFileStorage.delete: 文件不存在，幂等返回: %s", file_path)
            return
        except OSError as exc:
            raise DocumentStorageError(
                f"删除文件失败: {file_path}"
            ) from exc
        logger.info("LocalFileStorage.delete: 已删除 %s", dest_path)

    # ------------------------------------------------------------- 内部实现
    def _validate_filename(self, filename: str) -> str:
        """
        校验并返回安全的纯文件名。

        Raises:
            DocumentStoragePathTraversalError: 空文件名 / `.` / `..` / 含路径分隔符。
        """
        if not filename or filename in {".", ".."}:
            raise DocumentStoragePathTraversalError(
                f"非法的文件名: {filename!r}"
            )
        for sep in _PATH_SEPARATORS:
            if sep in filename:
                raise DocumentStoragePathTraversalError(
                    f"文件名禁止包含路径分隔符 {sep!r}: {filename!r}"
                )
        return filename

    def _resolve(self, relative_path: str) -> str:
        """
        将相对路径解析为 upload_dir 内的绝对路径，并做越界校验。

        Raises:
            DocumentStoragePathTraversalError: 空路径 / 解析后不在 upload_dir 内。
        """
        if not relative_path:
            raise DocumentStoragePathTraversalError("文件路径不能为空")

        base_dir = os.path.abspath(self._upload_dir)
        candidate = os.path.abspath(os.path.join(base_dir, relative_path))
        try:
            common = os.path.commonpath([base_dir, candidate])
        except ValueError:  # 不同盘符（Windows）等无法比较场景
            raise DocumentStoragePathTraversalError(
                f"路径越界: {relative_path!r}"
            ) from None
        if common != base_dir:
            raise DocumentStoragePathTraversalError(
                f"路径穿越拦截: {relative_path!r} 解析后位于 upload_dir 之外"
            )
        return candidate
