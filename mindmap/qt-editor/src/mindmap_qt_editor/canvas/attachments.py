"""在画布上拖入 SVG 或普通附件，并用系统默认应用打开已保存的附件。"""

from concurrent.futures import Future
from dataclasses import dataclass, replace
from os import fstat
from pathlib import Path
from stat import S_ISREG
from tempfile import TemporaryDirectory

from mindmap_core import FileAttachment, MindMapError, NodeGraph
from mindmap_core.formats import MAX_ARCHIVE_BYTES, validate_directory_capacity
from mindmap_core.layouts.graph import graph_child_position
from mindmap_core.models import SVG_MEDIA_TYPE, Asset
from mindmap_core.node_pages import parse_graph_page_id
from PySide6.QtCore import QRectF, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox
from shiboken6 import isValid

from .loading import _load_pool

# 系统打开请求异步发送，保留副本到进程退出，让外部应用有时间读取或编辑。
_opened_attachment_directories = []


@dataclass(frozen=True, slots=True)
class _AttachmentTarget:
    context: tuple[int, str | tuple[str, str | None] | None]
    graph: NodeGraph | None
    attachment: FileAttachment | None


@dataclass(slots=True)
class _AttachmentImport:
    node_id: str
    target: _AttachmentTarget
    future: Future[tuple[str, bytes]]
    svg: bool = False
    prepared: bool = False


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
    """文件读取完成后回到 GUI 线程，核对正式落点后提交。"""

    def _init_attachments(self):
        self._pending_attachment: _AttachmentImport | None = None
        self._attachment_timer = QTimer(self)
        self._attachment_timer.setInterval(20)
        self._attachment_timer.timeout.connect(self._poll_attachment)

    def _stop_attachment_read(self):
        self._attachment_timer.stop()
        pending, self._pending_attachment = self._pending_attachment, None
        if pending is not None:
            pending.future.cancel()
            self._sync_attachment_control(pending.node_id)

    def _stop_svg_drop(self):
        pending = self._pending_attachment
        if pending is not None and pending.svg:
            self._stop_attachment_read()

    def _sync_attachment_control(self, node_id: str):
        item = self.node_items.get(node_id)
        if item is not None and isValid(item):
            item._sync_optional_page_controls()

    def _pending_file_kind(self, node_id: str) -> str | None:
        pending = self._pending_attachment
        if pending is not None and pending.node_id == node_id and self._file_drop_is_current(pending):
            return "svg_import" if pending.svg else "attachment"
        return None

    def _file_drop_is_current(self, pending: _AttachmentImport) -> bool:
        return (
            not self._disposed
            and pending.node_id in self.document_view.nodes
            and pending.target.context == self._attachment_context()
            and pending.target.graph is self.graph
        )

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
            raise MindMapError("正在读取文件，请稍后重试")
        target = self._attachment_target(node_id)
        svg = path.suffix.casefold() == ".svg"
        if svg and target.graph is None:
            self._check_image_parent(node_id)
        future = _load_pool().submit(_read_attachment, path)
        self._pending_attachment = _AttachmentImport(node_id, target, future, svg=svg)
        self._sync_attachment_control(node_id)
        # 等到首次轮询再验证和提交，让画布先绘制导入状态。
        self._attachment_timer.start()
        return True

    def _prepare_attachment_target(self, pending: _AttachmentImport) -> _AttachmentTarget:
        self._verify_attachment_target(pending.node_id, pending.target)
        self._prepare_page_navigation()
        self._ensure_editable()
        target = self._attachment_target(pending.node_id)
        if target.context != pending.target.context:
            raise MindMapError("附件所属页面已切换，请重新拖入")
        if target.attachment != pending.target.attachment:
            raise MindMapError("导入期间附件目标发生变化，请重新拖入")
        return target

    def _poll_attachment(self):
        pending = self._pending_attachment
        if pending is None:
            return
        if pending.svg and not self._file_drop_is_current(pending):
            self._stop_attachment_read()
            return
        if (pending.prepared or pending.svg) and not pending.future.done():
            return
        self._attachment_timer.stop()
        try:
            if pending.svg:
                self.call(lambda: self._finish_svg_drop(pending, pending.future.result()[1]))
                return
            if not pending.prepared:
                target = self.call(lambda: self._prepare_attachment_target(pending))
                if target is None or self._pending_attachment is not pending:
                    return
                pending.target = target
                pending.prepared = True
                self._sync_attachment_control(pending.node_id)
            if not pending.future.done():
                self._attachment_timer.start()
                return
            self.call(
                lambda: self._finish_attachment_import(pending.node_id, pending.target, pending.future.result())
            )
        finally:
            if self._pending_attachment is pending and not self._attachment_timer.isActive():
                self._stop_attachment_read()

    def _add_svg_node(self, node_id, data, width, position=None):
        graph_owner_id = parse_graph_page_id(self.current_page_id)
        if graph_owner_id is None:
            return self.engine.add_image_child(node_id, data, SVG_MEDIA_TYPE, width=width)
        return self.engine.add_graph_image_node(graph_owner_id, node_id, data, width=width, position=position)

    def _preview_svg_drop(self, node_id, data, width):
        """试算容量与手动图坐标，退出候选事务后再提交正式节点。"""
        position = None
        with self.engine.preview_operation() as (candidate, _):
            key = self._add_svg_node(node_id, data, width)
            if self.graph is not None and self.graph.positions:
                page = candidate.page_view(self.current_page_id)
                _, sizes = self.factory.measure(page)
                layout = self._calculate_graph_layout(sizes, graph=page.graph, color=page.theme.accent)
                position = graph_child_position(layout, node_id, key, parents=page.graph.parent_groups())
                self.engine.set_node_graph(parse_graph_page_id(self.current_page_id),
                                           replace(page.graph, positions={**page.graph.positions, key: position}))
            validate_directory_capacity(candidate)
        return position

    def _finish_svg_drop(self, pending: _AttachmentImport, data: bytes):
        self._ensure_editable()
        node_id = pending.node_id
        asset = Asset.from_bytes(data, SVG_MEDIA_TYPE)
        _, size = self.factory.image_for_asset(asset)
        width = size.width()
        revision = self.engine.revision
        position = self._preview_svg_drop(node_id, data, width)
        self._prepare_page_navigation()
        if self._pending_attachment is not pending or self._attachment_context() != pending.target.context:
            return
        self._ensure_editable()
        if node_id not in self.document_view.nodes:
            raise MindMapError("SVG 的落点节点已失效")
        if revision != self.engine.revision:
            position = self._preview_svg_drop(node_id, data, width)
        key = self._add_svg_node(node_id, data, width, position)
        if self._attachment_context() == pending.target.context and not self._disposed:
            rect = self.layout_result.nodes.get(key)
            if rect is not None:
                self.select_nodes([key])
                self.view.ensureVisible(QRectF(rect.x, rect.y, rect.width, rect.height), 35, 35)
                self._update_history_view_after()
            if pending.target.graph is None:
                self.image_imported.emit(key)

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
