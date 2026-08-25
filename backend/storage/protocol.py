"""
FileStorage 抽象（Protocol）。

设计要点（与项目 Repository/Service Protocol 风格一致）：
1) 上层（Parser / Upload Service）依赖本 Protocol，而非具体 LocalFileStorage，
   便于单元测试 Mock 与未来切换对象存储实现。
2) save 返回「相对 upload_dir 的相对路径」，调用方可直接存入 Document.file_path，
   避免客户端传入完整/绝对路径（防路径穿越）。
3) resolve 将 Document.file_path 的逻辑路径转换为当前存储实现下 Parser 可直接
   读取的物理路径（本地实现为绝对路径；对象存储实现可为临时下载路径 / 对象键）。
4) delete 接收与 save 返回同一逻辑路径，删除存储层内文件；
   对不存在的文件保持幂等（不抛异常）。
5) 三者契约一致：save → 逻辑路径；resolve → 逻辑路径转物理路径；delete → 逻辑路径。
"""

from __future__ import annotations

from typing import Protocol


class FileStorage(Protocol):
    """文件存储抽象：保存与删除原始文件。"""

    def save(self, filename: str, data: bytes) -> str:
        """
        将字节数据保存到存储层。

        Args:
            filename: 客户端提交的原始文件名（可能含非法路径片段，实现必须消毒）。
            data: 文件字节内容。

        Returns:
            存储层内相对路径（可直接写入 Document.file_path，不含 upload_dir 前缀）。

        Raises:
            DocumentStorageError: 文件名不安全 / 写入失败（含路径穿越）。
        """
        ...

    def resolve(self, file_path: str) -> str:
        """
        将 Document.file_path 保存的逻辑路径解析为当前存储实现下
        Parser 可直接读取的物理路径。

        Args:
            file_path: save 返回的相对路径（逻辑路径）。

        Returns:
            物理路径：本地实现为绝对磁盘路径，对象存储实现可为
            临时下载路径 / 对象键（随实现而定）。

        Raises:
            DocumentStorageError: 路径越界（与 delete 相同的安全校验）。
        """
        ...

    def delete(self, file_path: str) -> None:
        """
        删除指定文件。

        Args:
            file_path: save 返回的相对路径（或 upload_dir 内相对路径）。

        Raises:
            DocumentStorageError: 路径越界 / 删除失败（非 FileNotFoundError 场景）。
            文件不存在时保持幂等（静默返回）。
        """
        ...
