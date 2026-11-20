"""在画布上读入文件，并用系统默认应用打开已保存的附件。"""

from dataclasses import dataclass
from os import fstat
from pathlib import Path
from stat import S_ISREG
from tempfile import TemporaryDirectory

from mindmap_core import FileAttachment, MindMapError, NodeGraph
from mindmap_core.formats import MAX_ARCHIVE_BYTES, validate_directory_capacity
from mindmap_core.node_pages import parse_graph_page_id
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox

from .loading import _load_pool

# 系统打开请求异步发送，保留副本到进程退出，让外部应用有时间读取或编辑。
_opened_attachment_directories = []


@dataclass(frozen=True, slots=True)
class _AttachmentTarget:
    context: tuple[int, str | tuple[str, str | None] | None]
    graph: NodeGraph | None
    attachment: FileAttachment | None


def _read_attachment(path: Path) -> tuple[str, bytes]:
    """在工作线程限量读取普通文件；文件在读取中发生变化时丢弃结果。"""
    try:
        with path.open("rb") as stream:
            before = fstat(stream.fileno())
            if not S_ISREG(before.st_mode):
                raise MindMapError("只能添加普通文件作为附件")
            if before.st_size > MAX_ARCHIVE_BYTES:
                raise MindMapError(f"附件「{path.name}」超过导图大小限制")
            data = stream.read(before.st_size + 1)
            after = fstat(stream.fileno())
    except OSError as error:
        raise MindMapError(f"无法读取附件「{path.name}」：{error.strerror or error}") from error
    if len(data) != before.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise MindMapError(f"读取期间附件「{path.name}」发生变化，请重新拖入")
    return path.name, data


class AttachmentActionsMixin:
    """文件读取完成后回到 GUI 线程，确认原附件仍属于同一节点。"""

    def _init_attachments(self):
        self._pending_attachment = None
        self._attachment_timer = QTimer(self)
        self._attachment_timer.setInterval(20)
        self._attachment_timer.timeout.connect(self._poll_attachment)

    def _stop_attachment_read(self):
        self._attachment_timer.stop()
        if self._pending_attachment is not None:
            self._pending_attachment[2].cancel()
            self._pending_attachment = None

    def _attachment_context(self):
        return self.engine.document_generation, self._view_key()

    def _attachment_target(self, node_id) -> _AttachmentTarget:
        node = self.document_view.nodes.get(node_id)
        if self._disposed or node is None:
            raise MindMapError("附件所属节点已失效")
        return _AttachmentTarget(self._attachment_context(), self.graph, node.attachment)

    def _verify_attachment_target(self, node_id, expected):
        self._ensure_editable()
        current = self._attachment_target(node_id)
        # 图文本可在编辑后重用局部 ID。图快照的身份变化使旧落点失效。
        if current.graph is not expected.graph or current != expected:
            raise MindMapError("导入期间附件目标发生变化，请重新拖入")

    def _start_attachment_import(self, node_id: str, path: Path) -> bool:
        self._ensure_editable()
        if self._pending_attachment is not None:
            raise MindMapError("正在读取附件，请稍后重试")
        context = self._attachment_context()
        self._prepare_page_navigation()
        target = self._attachment_target(node_id)
        if target.context != context:
            raise MindMapError("附件所属页面已切换，请重新拖入")
        self._pending_attachment = node_id, target, _load_pool().submit(_read_attachment, path)
        self._attachment_timer.start()
        return True

    def _poll_attachment(self):
        if self._pending_attachment is None:
            return
        node_id, target, future = self._pending_attachment
        if not future.done():
            return
        self._attachment_timer.stop()
        try:
            self.call(lambda: self._finish_attachment_import(node_id, target, future.result()))
        finally:
            self._pending_attachment = None

    def _finish_attachment_import(self, node_id, target, file):
        self._verify_attachment_target(node_id, target)
        previous = target.attachment
        if previous is not None:
            choice = QMessageBox.question(
                self, "替换附件", f"将附件「{previous.name}」替换为「{file[0]}」吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return
            self._verify_attachment_target(node_id, target)
        # 读取期间可能开始新的正文或侧栏编辑。沿用已有提交入口，避免丢弃新草稿。
        self._prepare_page_navigation()
        self._verify_attachment_target(node_id, target)
        graph_owner_id = parse_graph_page_id(self.current_page_id)
        with self.engine.preview_operation() as (candidate, _):
            self.engine.set_node_attachment(node_id, file, graph_owner_id=graph_owner_id)
            validate_directory_capacity(candidate)
        self.engine.set_node_attachment(node_id, file, graph_owner_id=graph_owner_id)

    def remove_node_attachment(self):
        """移除当前节点的附件，使用引擎的撤销历史。"""
        self._ensure_editable()
        node_id = self.current_node_id()
        self._prepare_page_navigation()
        self._attachment_target(node_id)
        self.engine.set_node_attachment(node_id, None, graph_owner_id=parse_graph_page_id(self.current_page_id))

    def open_node_attachment(self, node_id: str):
        """复制当前资源到独立临时目录，以原文件名交给系统。"""
        attachment = self._attachment_target(node_id).attachment
        if attachment is None:
            raise MindMapError("此节点没有附件")
        asset = self._book_document.assets.get(attachment.asset_id)
        if asset is None:
            raise MindMapError(f"附件「{attachment.name}」的资源缺失")
        directory = TemporaryDirectory(prefix="mindmap-attachment-", ignore_cleanup_errors=True)
        try:
            path = Path(directory.name) / attachment.name
            path.write_bytes(asset.data)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                raise MindMapError(f"无法打开附件「{attachment.name}」")
        except (OSError, MindMapError):
            directory.cleanup()
            raise
        _opened_attachment_directories.append(directory)
