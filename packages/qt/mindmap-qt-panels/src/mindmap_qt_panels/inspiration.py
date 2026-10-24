"""独立想法单选列表；内容、导航及跨控件移动均由所属会话宿主协调。"""

from collections.abc import Callable

from mindmap_core import MindMapEngine, MindMapError, Theme
from mindmap_qt_ui import toolbar_icon
from mindmap_qt_ui.ui_style import apply_ui_palette, dialog_styles, menu_styles, scrollbar_styles, ui_colors
from PySide6.QtCore import QMimeData, QModelIndex, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QContextMenuEvent, QDrag, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

# UserRole 继续保存宿主导航与拖放使用的根标识；额外角色仅供双行绘制读取。
_TITLE_ROLE = Qt.ItemDataRole.UserRole + 1
_SUBTITLE_ROLE = Qt.ItemDataRole.UserRole + 2


class _InspirationDelegate(QStyledItemDelegate):
    """参照收藏列表绘制双行想法，不接管列表的选择、导航或原生拖放事件。

    parent 为所属 InspirationPanel，仅读取其色板及宿主同步的当前根标识。
    所有几何均使用 Qt 逻辑像素，由 Qt 负责显示缩放，避免高 DPI 下重复缩放。
    """

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        """返回与收藏项一致的建议宽度及 58 像素行高，不受标题长度影响。

        option、index 保留 Qt 代理接口约定；实际绘制宽度由列表视口决定，长文字
        在 paint 中省略，完整双行文本与悬停提示仍由原列表项持有。
        """
        return QSize(180, 58)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        """依次绘制状态底色、灯泡、标题、子主题数量和键盘焦点，返回 None。

        当前项以宿主确认的根标识为准，不使用拖起或按下时临时改变的选中状态。
        当前底色优先于悬停底色，焦点轮廓独立显示；绘制后恢复调用方的画笔状态。
        """
        panel = self.parent()
        colors = panel._colors
        current = index.data(Qt.ItemDataRole.UserRole) == panel._current_root
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setClipRect(option.rect)
            # 窄侧栏中 Qt 的条目矩形仍可能保留建议宽度，省略号和焦点框须落在可见视口内。
            rect = QRect(option.rect)
            rect.setRight(min(rect.right(), panel.list_view.viewport().rect().right()))
            rect.adjust(3, 2, -3, -2)
            if current or option.state & QStyle.StateFlag.State_MouseOver:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(colors.selected if current else colors.hover))
                painter.drawRoundedRect(rect, 5, 5)
            icon = toolbar_icon("inspiration", colors.accent if current else colors.muted,
                                colors.muted, colors.accent)
            icon.paint(painter, QRect(rect.left() + 8, rect.top() + 10, 16, 16))
            # 图标、两行文字的起点和间距与收藏项保持一致；字体使用副本，避免修改 option。
            text_rect = rect.adjusted(32, 7, -8, -6)
            text_width = max(0, text_rect.width())
            font = QFont(option.font)
            font.setBold(current)
            painter.setFont(font)
            painter.setPen(QColor(colors.text))
            title = painter.fontMetrics().elidedText(index.data(_TITLE_ROLE),
                                                     Qt.TextElideMode.ElideRight, text_width)
            painter.drawText(QRect(text_rect.left(), text_rect.top(), text_width, 20),
                             Qt.AlignmentFlag.AlignVCenter, title)
            font.setBold(False)
            painter.setFont(font)
            painter.setPen(QColor(colors.muted))
            subtitle = painter.fontMetrics().elidedText(index.data(_SUBTITLE_ROLE),
                                                        Qt.TextElideMode.ElideRight, text_width)
            painter.drawText(QRect(text_rect.left(), text_rect.top() + 22, text_width, 18),
                             Qt.AlignmentFlag.AlignVCenter, subtitle)
            if option.state & QStyle.StateFlag.State_HasFocus:
                painter.setPen(QPen(QColor(colors.accent), 1, Qt.PenStyle.DotLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 4, 4)
        finally:
            painter.restore()


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
        self.setUniformItemSizes(True)
        self.setMouseTracking(True)
        self.setItemDelegate(_InspirationDelegate(panel))
        self.setAccessibleName("独立想法列表，单选")

    def mousePressEvent(self, event):
        self.dragged = False
        # 右键只确定菜单目标，不改变键盘选择；取消菜单后仍可继续操作原来的想法。
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
            return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        """消费列表菜单事件，分别按鼠标落点和键盘当前项确定稳定根标识。

        鼠标在空白处调用新建菜单；键盘菜单以当前项为准，并贴近该项弹出。
        不调用默认实现，防止事件冒泡后误弹停靠面板的布局菜单。
        """
        event.accept()
        item = (self.currentItem() if event.reason() == QContextMenuEvent.Reason.Keyboard
                else self.itemAt(event.pos()))
        position = event.globalPos()
        if item is not None and event.reason() == QContextMenuEvent.Reason.Keyboard:
            position = self.viewport().mapToGlobal(self.visualItemRect(item).bottomLeft())
        self.panel._show_context_menu(item.data(Qt.ItemDataRole.UserRole) if item else None, position)

    def startDrag(self, supported_actions):
        item = self.currentItem()
        provider = self.panel._create_drag
        if item is None or provider is None or not self.panel._editable:
            return
        self.dragged = True
        mime = provider(item.data(Qt.ItemDataRole.UserRole))
        if mime is None:
            return
        self.panel._set_drop_hint(False)
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
    new_action 由面板持有，供宿主标题栏和空态文字按钮共用；触发时仅发送
    new_requested，其图标、启用状态分别随 set_ui_colors、set_editable 同步。
    宿主可以代理动作，但不能转移其所有权或在 dispose 后继续触发。
    attach_requested、delete_requested 均携带右键命中的根标识，
    不借用画布当前选择；宿主负责权限复查、输入提交及可撤销的正式操作。
    menu_about_to_show 在菜单弹出前同步发出；宿主可据此更新输入法等即时权限。
    """

    navigate_requested = Signal(str)
    new_requested = Signal()
    attach_requested = Signal(str)
    delete_requested = Signal(str)
    menu_about_to_show = Signal()
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
        self._menu_root = self._menu_generation = None
        self.setObjectName("mindmap_inspiration_panel")
        self.setAccessibleName("灵感侧栏")
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        self.new_action = QAction("新建想法", self)
        self.new_action.setToolTip("新建想法")
        self.new_action.triggered.connect(self.new_requested)
        # 两个菜单均由面板持有，目标独立于当前画布与列表选择；空白处仅复用新建动作。
        self._menu = QMenu(self)
        self._menu.setToolTipsVisible(True)
        self.attach_action = self._menu.addAction("加入导图…")
        self._menu.addSeparator()
        self.delete_action = self._menu.addAction("删除想法")
        self.delete_action.setToolTip("删除整个想法及其子主题、附属页面，可撤销")
        for operation, action in (("attach", self.attach_action), ("delete", self.delete_action)):
            action.triggered.connect(lambda checked=False, key=operation: self._request_menu_action(key))
        # Qt 会先隐藏菜单，再触发 QAction；延后一轮清理，保留本次正常点击的目标。
        self._menu.aboutToHide.connect(lambda: QTimer.singleShot(0, self._clear_hidden_menu_target))
        self._empty_menu = QMenu(self)
        self._empty_menu.addAction(self.new_action)
        for menu in (self._menu, self._empty_menu):
            menu.aboutToShow.connect(self.menu_about_to_show)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("搜索想法标题")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("搜索想法标题")
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)
        self.count_label = QLabel(self)
        layout.addWidget(self.count_label)
        self.empty_state = QWidget(self)
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.setContentsMargins(8, 16, 8, 16)
        empty_layout.setSpacing(12)
        empty_layout.addStretch()
        self.empty_label = QLabel(self.empty_state)
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_label)
        # 空态按钮与标题栏复用同一动作；有想法时隐藏，只读时自动禁用。
        self.new_button = QToolButton(self.empty_state)
        self.new_button.setObjectName("inspiration_empty_new")
        self.new_button.setDefaultAction(self.new_action)
        self.new_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.new_button.setAccessibleName("新建想法")
        empty_layout.addWidget(self.new_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addStretch()
        layout.addWidget(self.empty_state, 1)
        self.list_view = _InspirationList(self)
        self.list_view.clicked.connect(self._clicked)
        self.list_view.activated.connect(self._activated)
        layout.addWidget(self.list_view, 1)
        self.drop_hint = QLabel(self)
        self.drop_hint.setObjectName("inspiration_drop_hint")
        self.drop_hint.setWordWrap(True)
        self.drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_hint.setMinimumHeight(48)
        self.drop_hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        # 仅在拖入合法分支或短暂提示成功时显示，常态隐藏且不占布局空间。
        self.drop_hint.hide()
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
        高亮依据此处同步的标识绘制，列表为拖放临时选中其他项时不会改变当前想法。
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
        self.list_view.viewport().update()

    def set_editable(self, editable: bool) -> None:
        """同步新建、菜单修改项与拖放权限，返回 None；不会修改引擎的内容或只读设置。

        False 会取消正在进行的侧栏拖动；搜索和单选导航仍可用。
        宿主负责在实际提交前再次检查权限和输入法；GUI 线程调用，释放后无操作。
        """
        if self._disposed:
            return
        self._editable = bool(editable) and not self._disposed
        self.new_action.setEnabled(self._editable)
        self.list_view.setDragEnabled(self._editable)
        self._sync_menu_actions()
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
        apply_ui_palette(self.list_view, colors)
        for menu in (self._menu, self._empty_menu):
            apply_ui_palette(menu, colors)
            menu.setStyleSheet(menu_styles(colors))
        self.new_action.setIcon(toolbar_icon("plus", colors.muted, colors.border, colors.accent))
        self.attach_action.setIcon(toolbar_icon("main", colors.muted, colors.border, colors.accent))
        self.delete_action.setIcon(toolbar_icon("clear", colors.error, colors.border, colors.error))
        self.setStyleSheet(
            f"QWidget#mindmap_inspiration_panel {{background:{colors.surface};}}"
            f"QLabel {{color:{colors.muted};background:transparent;}}"
            f"QListWidget {{background:{colors.surface};color:{colors.text};border:0;outline:0;}}"
            + dialog_styles(colors) + scrollbar_styles(colors)
            + f"QToolButton#inspiration_empty_new {{padding:6px 12px;border-color:{colors.border};}}")
        self._set_drop_hint(False)
        self.list_view.viewport().update()

    def flash_roots(self, node_ids: list[str]) -> None:
        """临时显示成功拆出的想法数量，1.6 秒后隐藏并释放提示所占的空间。

        node_ids 由宿主传入，仅读取数量，不写入引擎、不跳转画布或改变搜索、选择。
        GUI 线程调用，空列表或释放后无操作；后续拖放会取消本次提示的倒计时。
        """
        if self._disposed or not node_ids:
            return
        self._set_drop_hint(True, f"已新增 {len(node_ids)} 个独立想法")
        self._flash_timer.start(1600)

    def dispose(self) -> None:
        """幂等解除引擎订阅、计时器和宿主回调，宿主随后可安全删除控件。"""
        if self._disposed:
            return
        self._disposed = True
        self.close_popups()
        self.new_action.setEnabled(False)
        self._unsubscribe()
        if self._native_drag is not None:
            QDrag.cancel()
        self._flash_timer.stop()
        self._create_drag = self._accept_drag = self._drop_drag = None

    def close_popups(self) -> None:
        """关闭本面板菜单并清除目标，返回 None；可在释放后重复调用。

        宿主离开会话、替换文档或释放面板前调用，阻止旧菜单的迟到动作。
        不导航、不提交草稿、不更改文档；仅允许在所属 Qt GUI 线程调用。
        """
        self._menu_root = self._menu_generation = None
        self._menu.close()
        self._empty_menu.close()
        self._sync_menu_actions()

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        """将面板空白、无结果及空态区域的右键统一作为新建入口，不向停靠层冒泡。"""
        event.accept()
        self._show_context_menu(None, event.globalPos())

    def _show_context_menu(self, root_id: str | None, global_position: QPoint) -> None:
        """按命中根弹出专用菜单，只记录标识和文档代次，不切页、不修改选择。"""
        self.close_popups()
        if self._disposed:
            return
        if root_id is None:
            self._empty_menu.popup(global_position)
        elif any(root == root_id for root, _, _ in self._rows):
            self._menu_root, self._menu_generation = root_id, self.engine.document_generation
            self._sync_menu_actions()
            self._menu.popup(global_position)

    def _sync_menu_actions(self) -> None:
        """按目标有效性与修改权限统一更新迁入、删除动作。"""
        valid = (not self._disposed and self._menu_root is not None
                 and self._menu_generation == self.engine.document_generation
                 and any(root == self._menu_root for root, _, _ in self._rows))
        # 画布可能先收到删除事件并同步权限，此时侧栏摘要尚未刷新，需容忍目标已失效。
        try:
            if valid:
                self.engine.get_node(self._menu_root)
        except MindMapError:
            valid = False
        self.attach_action.setEnabled(valid and self._editable)
        self.delete_action.setEnabled(valid and self._editable)

    def _clear_hidden_menu_target(self) -> None:
        """菜单取消后一轮清除旧目标；新菜单已弹出时保留新目标。"""
        if not self._menu.isVisible():
            self._menu_root = self._menu_generation = None
            self._sync_menu_actions()

    def _request_menu_action(self, operation: str) -> None:
        """复查菜单目标与修改权限，再向宿主发送单个想法的操作意图。"""
        root, generation = self._menu_root, self._menu_generation
        self.close_popups()
        if (self._disposed or root is None or generation != self.engine.document_generation
                or not any(key == root for key, _, _ in self._rows)
                or not self._editable):
            return
        signals = {"attach": self.attach_requested, "delete": self.delete_requested}
        signals[operation].emit(root)

    def _refresh(self):
        rows = self.engine.inspiration_items()
        if rows != self._rows:
            scroll = self.list_view.verticalScrollBar().value()
            self.list_view.clear()
            for root, title, count in rows:
                title = title.replace("\n", " ") or "（空主题）"
                subtitle = f"{count} 个子主题"
                item = QListWidgetItem(f"{title}\n{subtitle}", self.list_view)
                item.setData(Qt.ItemDataRole.UserRole, root)
                item.setData(_TITLE_ROLE, title)
                item.setData(_SUBTITLE_ROLE, subtitle)
                item.setToolTip(f"{title}\n{count} 个普通子主题，可拖到画布主题上组合")
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
        self.empty_state.setVisible(not visible)
        self.empty_label.setText("没有匹配的想法" if self._rows else
                                 "还没有独立想法\n\n新建一个想法，\n或将导图分支拖到这里。")
        self.new_button.setVisible(not self._rows)
        self.list_view.setVisible(bool(visible))

    def _clicked(self, index):
        if not self.list_view.dragged:
            self._activated(index)

    def _activated(self, index):
        if not self._disposed and index.isValid():
            self.navigate_requested.emit(index.data(Qt.ItemDataRole.UserRole))

    def _engine_changed(self, event):
        if not self._disposed and event.kind != "saved":
            self.close_popups()
            if event.kind == "reset" and self._native_drag is not None:
                QDrag.cancel()
            self._refresh()

    def _set_drop_hint(self, active: bool, text: str | None = None) -> None:
        """更新临时拖放反馈；非活动时隐藏提示，不保留底部占位。

        每次更新先取消旧成功提示的倒计时，避免其关闭后续拖放的反馈。
        text 为可选成功提示；省略时显示拖入反馈，成功提示的计时由 flash_roots 启动。
        """
        if self._disposed or not isValid(self.drop_hint):
            return
        self._flash_timer.stop()
        if not active:
            self.drop_hint.hide()
            return
        colors = self._colors
        self.drop_hint.setText(text or "松手后成为独立想法")
        self.drop_hint.setStyleSheet(
            f"color:{colors.accent};"
            f"background:{colors.selected};"
            f"border:1px solid {colors.accent};"
            "border-radius:6px;padding:6px;")
        self.drop_hint.show()

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
