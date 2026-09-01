"""
LocalFileStorage 单元测试（Phase 2.10 Step 2）。

覆盖：
- save 成功（返回相对路径 + 文件真实落盘）
- 自动创建 upload_dir
- 路径穿越拦截（save：`..` / 路径分隔符 / 绝对路径；delete：越界相对路径）
- delete 成功
- delete 不存在文件幂等
- resolve：逻辑路径 → 绝对物理路径（Phase 2.12 Step 3.2）
  - 返回绝对路径、可直接 open()、与 save 落盘位置一致
  - 越界拦截（`../`、`..`、绝对路径、含 `/` 或 `\\` 的越界路径）
"""

from __future__ import annotations

import os
import tempfile
import unittest

from backend.core.exceptions import (
    DocumentStorageError,
    DocumentStoragePathTraversalError,
)
from backend.storage import LocalFileStorage


class LocalFileStorageTest(unittest.TestCase):
    """LocalFileStorage 本地磁盘存储测试（临时目录隔离）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.upload_dir = os.path.join(self._tmp.name, "uploads")
        self.storage = LocalFileStorage(self.upload_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------ save 成功
    def test_save_success(self) -> None:
        """保存成功：返回唯一对象键，文件真实写入 upload_dir。"""
        rel_path = self.storage.save("test.txt", b"hello world")

        self.assertNotEqual(rel_path, "test.txt")
        self.assertTrue(rel_path.endswith(".txt"))
        dest = os.path.join(self.upload_dir, rel_path)
        self.assertTrue(os.path.isfile(dest))
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), b"hello world")

    def test_same_filename_gets_unique_storage_keys(self) -> None:
        first = self.storage.save("same.txt", b"first")
        second = self.storage.save("same.txt", b"second")

        self.assertNotEqual(first, second)
        with open(self.storage.resolve(first), "rb") as fh:
            self.assertEqual(fh.read(), b"first")
        with open(self.storage.resolve(second), "rb") as fh:
            self.assertEqual(fh.read(), b"second")

    # ------------------------------------------------------ 自动创建目录
    def test_save_creates_missing_upload_dir(self) -> None:
        """upload_dir 不存在时 save 自动创建。"""
        self.assertFalse(os.path.isdir(self.upload_dir))

        rel_path = self.storage.save("a.txt", b"data")

        self.assertTrue(os.path.isdir(self.upload_dir))
        self.assertTrue(os.path.isfile(os.path.join(self.upload_dir, rel_path)))

    # ------------------------------------------------------ 路径穿越拦截
    def test_save_rejects_parent_traversal(self) -> None:
        """save 拒绝 `../` 路径穿越。"""
        with self.assertRaises(DocumentStoragePathTraversalError):
            self.storage.save("../evil.txt", b"x")

    def test_save_rejects_absolute_path(self) -> None:
        """save 拒绝绝对路径（含路径分隔符）。"""
        with self.assertRaises(DocumentStoragePathTraversalError):
            self.storage.save("/etc/passwd", b"x")

    def test_save_rejects_windows_path_separator(self) -> None:
        """save 拒绝 Windows 风格路径分隔符 `\\`（跨平台防御）。"""
        with self.assertRaises(DocumentStoragePathTraversalError):
            self.storage.save("..\\evil.txt", b"x")

    def test_save_rejects_dot_names(self) -> None:
        """save 拒绝 `.` / `..` 文件名。"""
        with self.assertRaises(DocumentStoragePathTraversalError):
            self.storage.save("..", b"x")
        with self.assertRaises(DocumentStoragePathTraversalError):
            self.storage.save(".", b"x")

    def test_delete_rejects_traversal_outside_upload_dir(self) -> None:
        """delete 拒绝解析后越出 upload_dir 的相对路径。"""
        with self.assertRaises(DocumentStoragePathTraversalError):
            self.storage.delete("../../outside.txt")

    def test_save_traversal_does_not_write_file(self) -> None:
        """路径穿越被拦截时，不创建目录、不产生任何文件（无副作用）。"""
        with self.assertRaises(DocumentStorageError):
            self.storage.save("../evil.txt", b"x")
        # save 先校验文件名再 makedirs/写盘，被拒绝后 upload_dir 不应被创建
        self.assertFalse(os.path.isdir(self.upload_dir))

    # ------------------------------------------------------------ delete
    def test_delete_success(self) -> None:
        """delete 删除真实存在的文件。"""
        rel_path = self.storage.save("del.txt", b"data")
        dest = os.path.join(self.upload_dir, rel_path)
        self.assertTrue(os.path.isfile(dest))

        self.storage.delete(rel_path)

        self.assertFalse(os.path.exists(dest))

    def test_delete_missing_file_is_idempotent(self) -> None:
        """delete 不存在文件时幂等返回（不抛异常）。"""
        # 不抛异常即通过
        self.storage.delete("ghost.txt")

    # ------------------------------------------------------------ resolve
    def test_resolve_returns_absolute_physical_path(self) -> None:
        """resolve 返回绝对物理路径。"""
        rel_path = self.storage.save("a.txt", b"data")
        physical = self.storage.resolve(rel_path)

        self.assertTrue(os.path.isabs(physical))
        self.assertEqual(
            physical,
            os.path.abspath(os.path.join(self.upload_dir, rel_path)),
        )
        self.assertTrue(physical.startswith(os.path.abspath(self.upload_dir)))

    def test_resolve_path_is_openable(self) -> None:
        """resolve 返回的路径可直接 open() 读取真实内容。"""
        rel_path = self.storage.save("open.txt", b"hello resolve")
        physical = self.storage.resolve(rel_path)

        with open(physical, "rb") as fh:
            self.assertEqual(fh.read(), b"hello resolve")

    def test_resolve_matches_save_location(self) -> None:
        """resolve 与 save 指向同一物理文件（位置一致）。"""
        rel_path = self.storage.save("same.txt", b"x")
        physical = self.storage.resolve(rel_path)
        saved = os.path.join(self.upload_dir, rel_path)

        self.assertEqual(physical, os.path.abspath(saved))
        self.assertTrue(os.path.isfile(physical))

    def test_resolve_rejects_parent_traversal(self) -> None:
        """resolve 拒绝 `../` 越界路径（与 delete 同一安全校验）。"""
        with self.assertRaises(DocumentStoragePathTraversalError):
            self.storage.resolve("../../outside.txt")

    def test_resolve_rejects_dot_dot(self) -> None:
        """resolve 拒绝 `..` 本身。"""
        with self.assertRaises(DocumentStoragePathTraversalError):
            self.storage.resolve("..")

    def test_resolve_rejects_absolute_path(self) -> None:
        """resolve 拒绝越出 upload_dir 的绝对路径。"""
        outside = os.path.abspath(
            os.path.join(self.upload_dir, "..", "evil.txt")
        )
        with self.assertRaises(DocumentStoragePathTraversalError):
            self.storage.resolve(outside)

    def test_resolve_rejects_separator_traversal(self) -> None:
        """resolve 拒绝含 `/` 或 `\\` 的越界路径。"""
        for bad in ("../evil.txt", "..\\evil.txt", "sub/../../evil.txt"):
            with self.subTest(path=bad):
                with self.assertRaises(DocumentStoragePathTraversalError):
                    self.storage.resolve(bad)


if __name__ == "__main__":
    unittest.main()
