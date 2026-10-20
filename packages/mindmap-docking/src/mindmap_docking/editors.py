"""可嵌套分屏的编辑器标签容器；文档与工具窗都使用稳定 token 进行迁移。"""

from uuid import uuid4

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QApplication, QHBoxLayout, QMenu, QTabBar, QTabWidget, QToolBar, QWidget

from .controls import DockSplitter, IconButton, _CardCorners
from .models import DockTarget


class _EditorCloseButton(IconButton):
    """页签栏拥有的关闭入口；保留控件及占位，只按交互状态切换图形。

    bar 为所属 EditorTabBar，title 只用于提示及无障碍名称。不拥有文档，关闭仍
    由编辑组按稳定 token 提交，业务的关闭确认、控件所有权和生命周期保持不变。
    """

    def __init__(self, bar, title):
        super().__init__(bar.group.manager, symbol="close", tooltip=f"关闭 {title}", parent=bar)
        self.bar = bar
        self._revealed = False
        self.setFixedSize(18, 18)

    def set_revealed(self, value):
        """仅改变绘制，不调用 hide 或改变尺寸，避免悬停时页签宽度跳动。"""
        if self._revealed != value:
            self._revealed = value
            self.update()

    def paintEvent(self, event):
        if self._revealed:
            super().paintEvent(event)

    def enterEvent(self, event):
        super().enterEvent(event)
        self.bar._hovered_index = self.bar.tabAt(self.mapTo(self.bar, self.rect().center()))
        self.bar._update_close_buttons()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.bar._hovered_index = -1
        self.bar._update_close_buttons()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.bar._update_close_buttons()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.bar._update_close_buttons()


class EditorTabBar(QTabBar):
    """文档卡片内的页签栏；Qt 继续处理标签文字、图标、滚动、键盘及拖动排序。"""

    def __init__(self, group):
        super().__init__(group)
        self.group = group
        self.press = QPoint()
        self.pressed_index = -1
        self._hovered_index = -1
        self.setObjectName(f"editor_tabs_{group.group_id}")
        self.setMouseTracking(True)
        self.setMovable(True)
        self.setExpanding(False)
        self.setDrawBase(False)
        self.setElideMode(Qt.TextElideMode.ElideRight)
        self.setIconSize(QSize(16, 16))
        self.currentChanged.connect(lambda *_: self._update_close_buttons())
        self.tabMoved.connect(self._tabs_moved)

    def refresh_theme(self):
        """应用仅作用于本栏的页签样式；工具窗内业务 QTabWidget 不受影响。"""
        theme = self.group.manager.theme
        if self.minimumHeight() != theme.header_height or self.maximumHeight() != theme.header_height:
            self.setFixedHeight(theme.header_height)
        font = self.font()
        size = 12 if theme.compact else 13
        if font.pixelSize() != size:
            font.setPixelSize(size)
            self.setFont(font)
        selector = f"QTabBar#{self.objectName()}"
        stylesheet = f"""
            {selector} {{ background:transparent; border:0; }}
            {selector}::tab {{ background:transparent; color:{theme.css('text-muted')};
                margin:0; padding:0 10px; border:0; border-radius:0; }}
            {selector}::tab:selected {{ background:transparent; color:{theme.css('text-default')}; }}
            {selector}::tab:hover {{ background:{theme.css('toolbar-bg-hovered')}; }}
        """
        # 普通工具切换也会刷新宿主，相同样式无需再次触发 Qt 样式重排。
        if self.styleSheet() != stylesheet:
            self.setStyleSheet(stylesheet)
        self.updateGeometry()
        self._update_close_buttons()
        self.update()

    def tabSizeHint(self, index):
        size = super().tabSizeHint(index)
        # 固定整行高度，使页签标题与工具窗标题对齐；宽度仍由 Qt 按图标、文字及关闭占位计算。
        return QSize(size.width(), self.group.manager.theme.header_height)

    def paintEvent(self, event):
        super().paintEvent(event)
        index = self.currentIndex()
        if index >= 0:
            rect = self.tabRect(index)
            painter = QPainter(self)
            # 使用逻辑坐标的 2 像素短线强调当前文档，不恢复胶囊底色或额外抬高页签。
            painter.fillRect(rect.left() + 10, rect.bottom() - 1, max(0, rect.width() - 20), 2,
                             self.group.manager.theme.color("control-brand-border"))

    def _update_close_buttons(self):
        """选中、鼠标悬停或键盘聚焦时绘制关闭图形；所有按钮始终保留固定占位。"""
        for index in range(self.count()):
            button = self.tabButton(index, QTabBar.ButtonPosition.RightSide)
            if isinstance(button, _EditorCloseButton):
                button.set_revealed(index in (self.currentIndex(), self._hovered_index) or button.hasFocus())

    def _tabs_moved(self, *_):
        self._hovered_index = -1
        self._update_close_buttons()

    def tabRemoved(self, index):
        super().tabRemoved(index)
        self._hovered_index = -1
        self._update_close_buttons()

    def enterEvent(self, event):
        super().enterEvent(event)
        self._hovered_index = self.tabAt(event.position().toPoint())
        self._update_close_buttons()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._hovered_index = -1
        self._update_close_buttons()

    def mousePressEvent(self, event):
        self.press = event.position().toPoint()
        self.pressed_index = self.tabAt(self.press)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        point = event.position().toPoint()
        self._hovered_index = self.tabAt(point)
        self._update_close_buttons()
        if event.buttons() & Qt.MouseButton.LeftButton and self.pressed_index >= 0 and (point - self.press).manhattanLength() >= QApplication.startDragDistance() and not self.rect().adjusted(0, -8, 0, 8).contains(point):
            token = self.tabData(self.pressed_index)
            self.pressed_index = -1
            if token:
                self.group.manager.start_document_drag(token, self, self.press)
                return
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.tabAt(event.position().toPoint()) >= 0:
            self.group.manager.hide_restore_all()

    def contextMenuEvent(self, event):
        index = self.tabAt(event.pos())
        if index < 0:
            return
        token = self.tabData(index)
        manager = self.group.manager
        menu = QMenu(self)
        menu.addAction("关闭", lambda: self.group.close_token(token))
        menu.addAction("关闭其他标签", lambda: self.group.close_others(token))
        menu.addSeparator()
        menu.addAction("向右拆分", lambda: self.group.area.split_token(token, Qt.Orientation.Horizontal))
        menu.addAction("向下拆分", lambda: self.group.area.split_token(token, Qt.Orientation.Vertical))
        menu.addAction("移至新框架", lambda: manager.detach_token(token))
        menu.exec(event.globalPos())


class EditorGroup(QTabWidget):
    """一个分屏对应一张文档卡片，页签栏与正文共享背景和外部圆角。

    area 拥有组，组拥有 Qt 页签容器和关闭按钮；文档内容仍由管理器登记并迁移。
    外观更新不创建、关闭或复制文档，不向布局快照增加呈现字段。
    """

    def __init__(self, area, group_id=None):
        super().__init__()
        self.area = area
        self.manager = area.manager
        self.group_id = group_id or uuid4().hex[:8]
        self.setObjectName(f"editor_group_{self.group_id}")
        self.setTabBar(EditorTabBar(self))
        # 角部控件交给 QTabWidget 分配空间，长标题仍可使用原生标签滚动按钮，
        # 无需覆盖页签或在文档正文内增加第二行工具栏。动作本身仍由业务持有。
        self._document_actions = QToolBar("文档操作", self)
        self._document_actions.setObjectName(f"editor_actions_{self.group_id}")
        self._document_actions.setAccessibleName("当前文档操作")
        self._document_actions.setMovable(False)
        self._document_actions.setFloatable(False)
        self._document_actions.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._document_actions.setIconSize(QSize(18, 18))
        self.setCornerWidget(self._document_actions, Qt.Corner.TopRightCorner)
        self._document_actions.hide()
        self.setDocumentMode(True)
        # QTabWidget 开启文档模式会重新打开 QTabBar 的原生底线，必须在其后关闭，
        # 否则深色卡片仍会出现由平台调色板绘制的亮边。
        self.tabBar().setDrawBase(False)
        self.setMinimumSize(180, 120)
        self.setAcceptDrops(True)
        self.currentChanged.connect(self._current_changed)
        self.tabBar().tabMoved.connect(lambda *_: self.manager.schedule_save())
        self._corners = _CardCorners(self, self.manager)
        self.refresh_theme()

    def refresh_theme(self):
        """刷新本组的圆角、页签与语义图标，保留选择、顺序和业务 QWidget 实例。"""
        self.tabBar().refresh_theme()
        theme = self.manager.theme
        self._document_actions.setFixedHeight(theme.header_height)
        selector = f"QToolBar#{self._document_actions.objectName()}"
        stylesheet = f"""
            {selector} {{ background:transparent; border:0; spacing:3px; padding:0 6px; }}
            {selector} QToolButton {{ background:transparent; color:{theme.css('text-muted')};
                border:1px solid transparent; border-radius:5px; padding:3px; }}
            {selector} QToolButton:hover {{ background:{theme.css('toolbar-bg-hovered')}; }}
            {selector} QToolButton:pressed, {selector} QToolButton:checked {{
                background:{theme.css('toolbar-selected-bg-active')}; }}
            {selector} QToolButton:focus {{ border-color:{theme.css('control-brand-border')}; }}
        """
        if self._document_actions.styleSheet() != stylesheet:
            self._document_actions.setStyleSheet(stylesheet)
        self._sync_document_actions()
        for index in range(self.count()):
            token = self.tabBar().tabData(index)
            spec = self.manager.specs.get(token[5:]) if isinstance(token, str) and token.startswith("tool:") else None
            document = self.manager.documents.get(token)
            if spec is not None or document is not None:
                self.setTabIcon(index, self.manager.theme.icon(spec.icon if spec else document["icon"], 16))
        self._corners.refresh()
        self.update()

    def _sync_document_actions(self):
        """复用当前文档的动作；切换到工具标签或空组时完整释放角部占位。"""
        token = self.tabBar().tabData(self.currentIndex()) if self.currentIndex() >= 0 else None
        document = self.manager.documents.get(token)
        actions = document.get("header_actions", ()) if document else ()
        if self._document_actions.actions() != list(actions):
            self._document_actions.clear()
            self._document_actions.addActions(actions)
            for action in actions:
                button = self._document_actions.widgetForAction(action)
                # 允许 Tab 键访问，同时避免鼠标点击夺走业务输入控件的焦点。
                button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
                button.setAccessibleName(action.text())
                button.setAccessibleDescription(action.toolTip())
        self._document_actions.setVisible(bool(actions))

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.manager.theme.color("editor-bg"))
        # 标题和正文位于同一张卡片，分隔线与工具窗标题采用相同的低对比颜色及边距。
        theme = self.manager.theme
        color = theme.color("ToolWindow.Header.borderColor", theme.color("tool-window-border").name())
        painter.fillRect(10, self.tabBar().height() - 1, max(0, self.width() - 20), 1, color)

    def add_token(self, token, widget, title, icon=None, index=-1):
        """迁移时先从旧标签容器移除，再添加同一个 QWidget，绝不复制业务内容。"""
        old = self.area.find_token(token)
        if old:
            old_group, old_index = old
            if old_group is self:
                self.setCurrentIndex(old_index)
                return
            old_group.removeTab(old_index)
        index = self.count() if index < 0 else min(index, self.count())
        inserted = self.insertTab(index, widget, icon or self.manager.theme.icon("document", 16), title)
        self.tabBar().setTabData(inserted, token)
        close = _EditorCloseButton(self.tabBar(), title)
        close.clicked.connect(lambda: self.close_token(token))
        self.tabBar().setTabButton(inserted, QTabBar.ButtonPosition.RightSide, close)
        self.setCurrentIndex(inserted)
        # insertTab 可能先发出 currentChanged，彼时稳定 token 尚未写入，需补同步。
        self._sync_document_actions()
        self.tabBar()._update_close_buttons()
        widget.show()

    def close_token(self, token):
        if token.startswith("tool:"):
            self.manager.hide(token[5:])
        else:
            self.manager.close_document(token)

    def close_others(self, token):
        for index in reversed(range(self.count())):
            other = self.tabBar().tabData(index)
            if other != token:
                self.close_token(other)

    def _current_changed(self, index):
        self._sync_document_actions()
        if index < 0 or self.manager.refreshing:
            return
        token = self.tabBar().tabData(index)
        if isinstance(token, str) and token.startswith("tool:"):
            self.manager.mark_active(token[5:])
        self.manager.schedule_save()

    def dragEnterEvent(self, event):
        if self.manager.decode_drag(event.mimeData()):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        data = self.manager.decode_drag(event.mimeData())
        if data:
            if data["kind"] == "tool":
                host = self.area.host
                point = self.mapTo(host, event.position().toPoint())
                target, rect, caption = host.drop_target(point)
                if event.position().y() <= self.tabBar().height() + 4:
                    rect = self.geometry()
                    rect.moveTopLeft(self.mapTo(host, QPoint()))
                    caption = "作为编辑器标签"
                host.preview.present(rect, caption)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self.area.host.preview.hide()

    def dropEvent(self, event):
        data = self.manager.decode_drag(event.mimeData())
        if not data:
            return
        index = self.tabBar().tabAt(self.tabBar().mapFrom(self, event.position().toPoint()))
        if data["kind"] == "tool":
            # 编辑器正文也必须允许边缘停靠；仅标签栏命中优先解释为编辑器标签。
            host = self.area.host
            target, _, _ = host.drop_target(self.mapTo(host, event.position().toPoint()))
            if event.position().y() <= self.tabBar().height() + 4 or target.position == "editor":
                target = DockTarget("editor", host.host_id, index, self.group_id)
            self.manager.move(data["id"], target)
        else:
            self.manager.move_document(data["id"], self.area.host.host_id, self.group_id, index)
        self.manager.drag_committed = True
        self.area.host.preview.hide()
        event.acceptProposedAction()


class EditorArea(QWidget):
    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.manager = host.manager
        self.setObjectName(f"editor_{host.host_id}")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self.root = DockSplitter(Qt.Orientation.Horizontal, self.manager)
        self._layout.addWidget(self.root)
        self.groups = []
        self._new_group(parent=self.root)

    def _new_group(self, parent=None, group_id=None):
        group = EditorGroup(self, group_id)
        self.groups.append(group)
        if parent:
            parent.addWidget(group)
        return group

    def group(self, group_id=""):
        return next((group for group in self.groups if group.group_id == group_id), self.groups[0])

    def find_token(self, token):
        for host in self.manager.hosts.values():
            for group in host.editor.groups:
                for index in range(group.count()):
                    if group.tabBar().tabData(index) == token:
                        return group, index
        return None

    def split_token(self, token, orientation):
        location = self.find_token(token)
        if not location:
            return
        original, index = location
        parent = original.parentWidget()
        while not isinstance(parent, DockSplitter):
            parent = parent.parentWidget()
        position = parent.indexOf(original)
        branch = DockSplitter(orientation, self.manager)
        original.setParent(None)
        parent.insertWidget(position, branch)
        branch.addWidget(original)
        group = self._new_group(parent=branch)
        if token.startswith("tool:"):
            self.manager.move(token[5:], DockTarget("editor", self.host.host_id, -1, group.group_id))
        else:
            self.manager.move_document(token, self.host.host_id, group.group_id)
        branch.setSizes([500, 500])
        self.manager.schedule_save()

    def snapshot(self):
        def walk(widget):
            if isinstance(widget, EditorGroup):
                return {"kind": "tabs", "id": widget.group_id, "tokens": [widget.tabBar().tabData(n) for n in range(widget.count())], "current": widget.currentIndex()}
            return {"kind": "split", "orientation": "horizontal" if widget.orientation() == Qt.Orientation.Horizontal else "vertical", "sizes": widget.sizes(), "children": [walk(widget.widget(n)) for n in range(widget.count())]}
        return walk(self.root)

    def refresh_theme(self):
        """统一更新嵌套分割器间距及每张文档卡片的外观，不改变分屏树和内部比例。"""
        self.root.refresh_theme()
        for splitter in self.root.findChildren(DockSplitter):
            splitter.refresh_theme()
        for group in self.groups:
            group.refresh_theme()

    def restore_structure(self, data):
        """重建容器树前把文档放入停车容器，防止删除旧分屏时级联销毁内容。"""
        for group in self.groups:
            while group.count():
                page = group.widget(0)
                group.removeTab(0)
                page.hide()
                page.setParent(self.manager.parking)
        self.groups.clear()
        old_root = self.root
        self._layout.removeWidget(old_root)
        old_root.hide()
        old_root.setParent(None)
        old_root.deleteLater()
        def build(node):
            if node.get("kind") == "tabs":
                group = self._new_group(group_id=str(node.get("id", uuid4().hex[:8])))
                group._restore_tokens = list(node.get("tokens", []))
                group._desired_order = list(group._restore_tokens)
                group._restore_current = int(node.get("current", 0))
                tokens = node.get("tokens", [])
                current = group._restore_current
                group._desired_selection = tokens[current] if 0 <= current < len(tokens) else ""
                return group
            split = DockSplitter(Qt.Orientation.Vertical if node.get("orientation") == "vertical" else Qt.Orientation.Horizontal, self.manager)
            for child in node.get("children", [])[:16]:
                split.addWidget(build(child))
            if node.get("sizes"):
                split.setSizes(node["sizes"])
            return split
        restored = build(data)
        if isinstance(restored, EditorGroup):
            self.root = DockSplitter(Qt.Orientation.Horizontal, self.manager)
            self.root.addWidget(restored)
        else:
            self.root = restored
        self._layout.addWidget(self.root)
        if not self.groups:
            self._new_group(parent=self.root)

    def restore_documents(self):
        for group in self.groups:
            for token in getattr(group, "_restore_tokens", []):
                if token in self.manager.documents:
                    doc = self.manager.documents[token]
                    group.add_token(token, doc["widget"], doc["title"], self.manager.theme.icon(doc["icon"], 16))
            current = getattr(group, "_restore_current", 0)
            if group.count():
                group.setCurrentIndex(min(current, group.count() - 1))
