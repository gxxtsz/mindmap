"""独立想法单选列表；内容、导航及跨控件移动均由所属会话宿主协调。"""

from collections.abc import Callable

from mindmap_core import MindMapEngine, Theme
from mindmap_qt_ui.ui_style import apply_ui_palette, dialog_styles, scrollbar_styles, ui_colors
from PySide6.QtCore import QMimeData, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid


class _InspirationList(QListWidget):
    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self.dragged = False
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(False)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setAccessibleName("独立想法列表，单选")

    def mousePressEvent(self, event):
        self.dragged = False
        super().mousePressEvent(event)

    def startDrag(self, supported_actions):
        item = self.currentItem()
        provider = self.panel._create_drag
        if item is None or provider is None or not self.panel._editable:
            return
        self.dragged = True
        mime = provider(item.data(Qt.ItemDataRole.UserRole))
        if mime is None:
            return
        drag = QDrag(self)
        drag.setMimeData(mime)
        self.panel._native_drag = drag
        try:
            drag.exec(Qt.DropAction.MoveAction)
        finally:
            self.panel._native_drag = None
            if not self.panel._disposed and isValid(self.panel):
                self.panel._set_drop_hint(False)
                self.panel.drag_finished.emit()
            if isValid(drag):
                drag.deleteLater()

    def dragEnterEvent(self, event):
        self.panel.dragEnterEvent(event)

    def dragMoveEvent(self, event):
        self.panel.dragMoveEvent(event)

    def dragLeaveEvent(self, event):
        self.panel.dragLeaveEvent(event)

    def dropEvent(self, event):
        self.panel.dropEvent(event)


class InspirationPanel(QWidget):
    """每份文档一个灵感面板，不依赖画布或停靠包，仅通过信号和回调交给宿主。

    engine 必须在 dispose 前存活，全部方法在 Qt GUI 线程调用。面板订阅已提交
    摘要，绝不直接改写文档。navigate_requested(root_id)、new_requested 仅表达
    用户意图；宿主执行输入检查、单个想法导航和正式事务。
    """

    navigate_requested = Signal(str)
    new_requested = Signal()
    # 原生拖放结束或取消后通知宿主清理预览，兼容 Windows 不补发离开事件的情况。
    drag_finished = Signal()

    def __init__(self, engine: MindMapEngine, parent: QWidget | None = None):
        """建立单选列表与搜索；不新建文件或节点，宿主负责最终控件所有权。"""
        super().__init__(parent)
        self.engine = engine
        self._disposed, self._editable = False, True
        self._create_drag = self._accept_drag = self._drop_drag = None
        self._current_root = None
        self._rows = ()
        self._native_drag = None
        self.setObjectName("mindmap_inspiration_panel")
        self.setAccessibleName("灵感侧栏")
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        header = QHBoxLayout()
        self.new_button = QToolButton(self)
        self.new_button.setText("＋ 新建想法")
        self.new_button.clicked.connect(self.new_requested)
        header.addWidget(self.new_button)
        header.addStretch()
        layout.addLayout(header)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("搜索想法标题")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("搜索想法标题")
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)
        self.count_label = QLabel(self)
        layout.addWidget(self.count_label)
        self.empty_label = QLabel("还没有独立想法\n\n点击“新建想法”，\n或将导图分支拖到这里。", self)
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_label)
        self.list_view = _InspirationList(self)
        self.list_view.clicked.connect(self._clicked)
        self.list_view.activated.connect(self._activated)
        layout.addWidget(self.list_view, 1)
        self.drop_hint = QLabel("将分支拖到这里，成为独立想法", self)
        self.drop_hint.setObjectName("inspiration_drop_hint")
        self.drop_hint.setWordWrap(True)
        self.drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_hint.setMinimumHeight(48)
        self.drop_hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.drop_hint)
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(lambda: self._set_drop_hint(False))
        self._unsubscribe = engine.subscribe(self._engine_changed)
        self.destroyed.connect(self._unsubscribe)
        self.set_ui_colors(None)
        self._refresh()

    def set_drag_handlers(self, create: Callable[[str], QMimeData | None],
                          accept: Callable[[QMimeData], bool], drop: Callable[[QMimeData], bool]) -> None:
        """注入本文件的拖放适配，返回 None；回调不访问其他文件或借用内部字段。

        create 在越过拖动阈值时读取新载荷；accept 必须只读且可重复调用；drop
        只在松手时执行一次正式事务并返回是否成功。宿主负责异常提示、权限和
        版本复查。dispose 会清除这些引用，不持有已关闭会话。
        """
        if not self._disposed:
            self._create_drag, self._accept_drag, self._drop_drag = create, accept, drop

    def set_current_root_id(self, root_id: str | None) -> None:
        """同步活动想法高亮，None 或不存在的根清空，返回 None。

        root_id 仅决定显示，不向引擎写选择或发送导航请求；搜索与滚动保持不变。
        宿主在导航或文档提交后于 GUI 线程调用，释放后无操作。
        """
        if self._disposed:
            return
        self._current_root = root_id
        scroll = self.list_view.verticalScrollBar().value()
        self.list_view.clearSelection()
        self.list_view.setCurrentRow(-1)
        for row in range(self.list_view.count()):
            if self.list_view.item(row).data(Qt.ItemDataRole.UserRole) == root_id:
                self.list_view.setCurrentRow(row)
                break
        self.list_view.verticalScrollBar().setValue(scroll)

    def set_editable(self, editable: bool) -> None:
        """同步新建与拖放权限，返回 None；不会修改引擎的内容或只读设置。

        False 会取消正在进行的侧栏拖动；搜索和单选导航仍可用。
        宿主负责在实际提交前再次检查权限和输入法；GUI 线程调用，释放后无操作。
        """
        if self._disposed:
            return
        self._editable = bool(editable) and not self._disposed
        self.new_button.setEnabled(self._editable)
        self.list_view.setDragEnabled(self._editable)
        if not self._editable:
            self._set_drop_hint(False)
            if self._native_drag is not None:
                QDrag.cancel()

    def set_ui_colors(self, colors) -> None:
        """应用宿主 UiColors 色板，None 使用默认浅色，返回 None。

        仅刷新本面板及其子控件，不更改全局 QApplication、导图主题或历史。
        GUI 线程调用，释放后无操作；传入色板由宿主保持有效且不能在此期间改写。
        """
        if self._disposed:
            return
        self._colors = colors or ui_colors(Theme())
        colors = self._colors
        apply_ui_palette(self, colors)
        self.setStyleSheet(
            f"QWidget#mindmap_inspiration_panel {{background:{colors.surface};}}"
            f"QLabel {{color:{colors.muted};background:transparent;}}"
            f"QListWidget {{background:{colors.surface};color:{colors.text};border:0;outline:0;}}"
            "QListWidget::item {padding:7px 9px;border-radius:5px;}"
            f"QListWidget::item:selected {{background:{colors.selected};color:{colors.text};}}"
            f"QListWidget::item:hover {{background:{colors.hover};}}"
            + dialog_styles(colors) + scrollbar_styles(colors))
        self._set_drop_hint(False)

    def flash_roots(self, node_ids: list[str]) -> None:
        """提示成功拆出的想法数量；保留画布、列表单选和搜索，不自动跳转。"""
        if self._disposed or not node_ids:
            return
        self._set_drop_hint(True, f"已新增 {len(node_ids)} 个独立想法")
        self._flash_timer.start(1600)

    def dispose(self) -> None:
        """幂等解除引擎订阅、计时器和宿主回调，宿主随后可安全删除控件。"""
        if self._disposed:
            return
        self._disposed = True
        self._unsubscribe()
        if self._native_drag is not None:
            QDrag.cancel()
        self._flash_timer.stop()
        self._create_drag = self._accept_drag = self._drop_drag = None

    def _refresh(self):
        rows = self.engine.inspiration_items()
        if rows != self._rows:
            scroll = self.list_view.verticalScrollBar().value()
            self.list_view.clear()
            for root, title, count in rows:
                title = title.replace("\n", " ") or "（空主题）"
                item = QListWidgetItem(f"{title}\n{count} 个子主题", self.list_view)
                item.setData(Qt.ItemDataRole.UserRole, root)
                item.setToolTip(f"{title}\n{count} 个普通子主题，可拖到画布主题上组合")
                item.setSizeHint(QSize(160, 56))
            self._rows = rows
            self.set_current_root_id(self._current_root)
            self.list_view.verticalScrollBar().setValue(scroll)
        self._filter()

    def _filter(self, *_):
        text = self.search.text().casefold()
        visible = 0
        for row, (_, title, _) in enumerate(self._rows):
            matched = text in title.casefold()
            self.list_view.item(row).setHidden(not matched)
            visible += matched
        self.count_label.setText(f"{len(self._rows)} 个独立想法" + (f" · 找到 {visible} 个" if text else ""))
        self.empty_label.setVisible(not visible)
        self.empty_label.setText("没有匹配的想法" if self._rows else
                                 "还没有独立想法\n\n点击“新建想法”，\n或将导图分支拖到这里。")
        self.list_view.setVisible(bool(visible))

    def _clicked(self, index):
        if not self.list_view.dragged:
            self._activated(index)

    def _activated(self, index):
        if not self._disposed and index.isValid():
            self.navigate_requested.emit(index.data(Qt.ItemDataRole.UserRole))

    def _engine_changed(self, event):
        if not self._disposed and event.kind != "saved":
            if event.kind == "reset" and self._native_drag is not None:
                QDrag.cancel()
            self._refresh()

    def _set_drop_hint(self, active, text=None):
        if self._disposed or not isValid(self.drop_hint):
            return
        colors = self._colors
        self.drop_hint.setText(text or "松手后成为独立想法" if active else "将分支拖到这里，成为独立想法")
        self.drop_hint.setStyleSheet(
            f"color:{colors.accent if active else colors.muted};"
            f"background:{colors.selected if active else colors.field};"
            f"border:1px {'solid' if active else 'dashed'} {colors.accent if active else colors.border};"
            "border-radius:6px;padding:6px;")

    def dragEnterEvent(self, event):
        if self._editable and self._accept_drag and self._accept_drag(event.mimeData()):
            self._set_drop_hint(True)
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            self._set_drop_hint(False)
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dragLeaveEvent(self, event):
        self._set_drop_hint(False)
        event.accept()

    def dropEvent(self, event):
        self._set_drop_hint(False)
        if (self._editable and self._accept_drag and self._drop_drag
                and self._accept_drag(event.mimeData()) and self._drop_drag(event.mimeData())):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()
