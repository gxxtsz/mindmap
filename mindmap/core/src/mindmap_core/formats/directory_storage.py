"""导图目录的版本检查和磁盘事务；不解释页面及资源的业务数据。"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import NamedTuple

from mindmap_core import MindMapError

from .limits import MAX_ARCHIVE_BYTES

MANIFEST = "mindmap.json"
_PAGE_FILE = r"(?:main|inspiration|[a-f0-9]{32}|[a-f0-9]{64})\.json"
_ASSET_FILE = r"[a-f0-9]{64}"
_MEMBER = re.compile(rf"(?:{re.escape(MANIFEST)}|pages/{_PAGE_FILE}|assets/{_ASSET_FILE})")
_JOURNAL = "恢复.json"


class DirectoryVersion(NamedTuple):
    """已保存导图的版本，包含每个受管文件的相对路径、大小及 SHA-256。

    同名文件内容相同即视为同一版本。工作区内的 Git 元数据及其他根目录文件
    不参与比较。可将此值传递给后台进程，文件位置由调用方另行保存。
    """

    files: tuple[tuple[str, int, str], ...]

    @property
    def size(self) -> int:
        """返回正文、页面和图片占用的磁盘字节数，不读取文件。"""
        return sum(size for _, size, _ in self.files)

    @property
    def has_manifest(self) -> bool:
        """返回此版本是否包含导图入口文件，供调用方在覆盖前检查。"""
        return any(name == MANIFEST for name, _, _ in self.files)


class DirectoryVersionConflict(MindMapError):
    """磁盘已变化或目标已移走；调用方可请求重新加载或另存内存中的快照。"""


def managed_files(root: Path) -> dict[str, int] | None:
    """列出根清单和受管目录中的普通文件大小；目录不存在时返回 None。

    使用规范的文件名，拒绝页面或资源中的链接和嵌套目录。Git 配置及根目录中
    的其他文件不属于导图。此查询不读取文件内容，也不修改目录。
    """
    if not root.exists():
        return None
    if not root.is_dir():
        raise MindMapError("请选择思维导图目录")
    manifest = root / MANIFEST
    paths = [manifest] if manifest.exists() or manifest.is_symlink() else []
    for name in ("pages", "assets"):
        path = root / name
        if path.is_symlink() or path.is_junction():
            raise MindMapError(f"导图的 {name} 必须是普通目录")
        if path.exists():
            if not path.is_dir():
                raise MindMapError(f"导图的 {name} 必须是普通目录")
            paths.extend(path.iterdir())
    files = {}
    for path in paths:
        name = path.relative_to(root).as_posix()
        if not _MEMBER.fullmatch(name) or path.is_symlink() or path.is_junction() or not path.is_file():
            raise MindMapError(f"导图目录包含无效的文件：{name}")
        files[name] = path.stat().st_size
    return dict(sorted(files.items()))


def disk_version(root: Path) -> DirectoryVersion | None:
    """读取受管文件的实际内容摘要；调用方在必要时先取得目录锁并完成恢复。"""
    files = managed_files(root)
    if files is None:
        return None
    if sum(files.values()) > MAX_ARCHIVE_BYTES:
        raise MindMapError("导图目录大小超过读取限制")
    entries = []
    for name, size in files.items():
        with (root / name).open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if os.fstat(stream.fileno()).st_size != size:
                raise DirectoryVersionConflict("读取期间导图目录已被外部修改")
        entries.append((name, size, digest))
    return DirectoryVersion(tuple(entries))


def require_version(root: Path, expected: DirectoryVersion | None) -> None:
    """确认目录仍与预期相同；None 要求目录尚未创建，结构被破坏也视为冲突。"""
    try:
        current = disk_version(root)
    except (MindMapError, FileNotFoundError) as exc:
        raise DirectoryVersionConflict(f"磁盘导图目录已被外部修改：{exc}") from exc
    if current != expected:
        message = "原导图目录已移走或删除" if current is None else "磁盘导图目录已被外部修改"
        raise DirectoryVersionConflict(message)


@contextmanager
def directory_lock(root: Path) -> Iterator[Path]:
    """对同一规范路径的应用读写和恢复互斥，返回同卷暂存目录的位置。

    进程退出时操作系统释放锁。锁文件位于系统临时目录，暂存及恢复数据位于
    导图的父目录，工作区无须跟踪这些文件。正被其他进程使用时返回可重试错误。
    """
    key = hashlib.sha256(os.fsencode(os.path.normcase(str(root)))).hexdigest()
    lock_dir = Path(tempfile.gettempdir()) / "mindmap-directory-locks"
    lock_dir.mkdir(exist_ok=True)
    with (lock_dir / key).open("a+b") as stream:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            def lock():
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)

            def unlock():
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def lock():
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def unlock():
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        try:
            lock()
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            raise MindMapError("导图目录正在读写，请稍后重试") from exc
        try:
            staging = root.parent / f".mindmap-save-{key}"
            if staging == root or staging.resolve().parent != root.parent or staging.is_symlink() or staging.is_junction():
                raise MindMapError("导图暂存目录位置无效")
            yield staging
        finally:
            unlock()


def _write_synced(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _copy_synced(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as incoming, target.open("wb") as outgoing:
        shutil.copyfileobj(incoming, outgoing)
        outgoing.flush()
        os.fsync(outgoing.fileno())


def _remove_staging(staging: Path) -> None:
    # 清理失败可以在下一次使用时重试；恢复标记已移除，不能再次回滚。
    if staging.is_dir() and not staging.is_symlink() and not staging.is_junction():
        with suppress(OSError):
            shutil.rmtree(staging)


def recover_directory(root: Path, staging: Path) -> None:
    """持锁恢复上一次未提交的写入；恢复完成后才能读取或更新导图。

    恢复标记出现前只有暂存文件。标记存在时保留备份到恢复全部完成，即使
    恢复过程中再次中断也能重试。标记移除后，暂存清理不影响磁盘上的版本。
    """
    marker = staging / _JOURNAL
    if marker.is_file():
        records = json.loads(marker.read_bytes())
        if (not isinstance(records, dict) or not records
                or any(not _MEMBER.fullmatch(name) or action not in ("添加", "替换", "删除")
                       for name, action in records.items())):
            raise MindMapError("导图保存恢复记录损坏")
        # 先核对路径类型，避免在外部替换了受管目录或文件后沿链接写入。
        if not root.exists():
            raise DirectoryVersionConflict("待恢复的导图目录已移走或删除")
        managed_files(root)
        # 只回滚已经替换或移走的文件。暂存内容仍存在时，该步尚未写入正式目录。
        for name, action in records.items():
            target = root / name
            if action == "删除":
                backup = staging / "删除" / name
                if not backup.exists():
                    continue
            else:
                if (staging / "写入" / name).exists():
                    continue
                if action == "添加":
                    target.unlink(missing_ok=True)
                    continue
                backup = staging / "备份" / name
            scratch = staging / "还原.tmp"
            _copy_synced(backup, scratch)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(scratch, target)
        marker.unlink()
    if staging.is_dir():
        _remove_staging(staging)


def commit_directory(
    root: Path, staging: Path, payload: Mapping[str, bytes], version: DirectoryVersion,
    current: DirectoryVersion | None,
) -> None:
    """持锁提交新快照；相同的文件原样保留，删除的页面和图片一并提交。

    新目录整体就位。已有目录先备份涉及的文件，在第一次替换前再次核对全部
    内容；恢复标记移除前的失败可撤销，标记移除后即为已提交。
    """
    previous = {name: digest for name, _, digest in current.files} if current is not None else {}
    desired = {name: digest for name, _, digest in version.files}
    updates = [name for name in desired if desired[name] != previous.get(name)]
    deletions = sorted(previous.keys() - desired.keys())
    if not updates and not deletions:
        return
    staging.mkdir()
    incoming = staging / "写入"
    try:
        for name in updates:
            _write_synced(incoming / name, payload[name])
        if current is None:
            require_version(root, None)
            incoming.rename(root)
            return
        records = {name: "替换" if name in previous else "添加" for name in updates}
        records.update(dict.fromkeys(deletions, "删除"))
        for name in updates:
            if name in previous:
                _copy_synced(root / name, staging / "备份" / name)
        _write_synced(staging / "待提交.json", json.dumps(records, ensure_ascii=False).encode("utf-8"))
        require_version(root, current)
        os.replace(staging / "待提交.json", staging / _JOURNAL)
        for name in deletions:
            removed = staging / "删除" / name
            removed.parent.mkdir(parents=True, exist_ok=True)
            os.replace(root / name, removed)
        # 清单最后更新。原子替换消耗暂存文件，恢复据此判断每一步是否发生。
        for name in sorted(updates, key=lambda name: name == MANIFEST):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(incoming / name, target)
        (staging / _JOURNAL).unlink()
    except (OSError, MindMapError):
        recover_directory(root, staging)
        raise
    finally:
        if not (staging / _JOURNAL).exists():
            _remove_staging(staging)
