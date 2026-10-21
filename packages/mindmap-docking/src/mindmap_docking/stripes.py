"""左右工具窗栏：用同一份几何结果管理停靠分组、溢出与拖放命中。"""

from dataclasses import dataclass, field

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QApplication, QMenu, QSizePolicy, QToolTip, QWidget

from .controls import IconButton
from .models import POSITION_LABELS, DockPosition, DockTarget


@dataclass
class _StripeLayout:
    """一次侧栏布局的值对象，不持有控件或业务内容。

    矩形全部使用侧栏局部逻辑坐标；members 包含可用入口，visible 保留原组内顺序。
    绘制、控件定位和命中均读取这个结果，避免 Qt 布局与拖放各算一套边界。
    """

    members: dict[DockPosition, tuple[str, ...]] = field(default_factory=dict)
    visible: dict[DockPosition, tuple[str, ...]] = field(default_factory=dict)
    buttons: dict[str, QRect] = field(default_factory=dict)
    groups: dict[DockPosition, QRect] = field(default_factory=dict)
    drop_areas: dict[DockPosition, QRect] = field(default_factory=dict)
    separator: QRect = field(default_factory=QRect)
    more: QRect = field(default_factory=QRect)


class _StripeEntryButton(IconButton):
    """侧栏工具与更多入口共用的绘制规格；生命周期由所属侧栏管理。

    点击区域由侧栏布局统一定位。入口只显示图标，状态背景普通为 32 方格、
    紧凑为 28 方格；工具名称通过悬停提示和无障碍名称提供。
    """

    def sizeHint(self):
        theme = self.manager.theme
        return QSize(theme.rail_width, theme.button_height)

    def _background_rect(self):
        size = 28 if self.manager.theme.compact else 32
        width = min(size, self.width())
        height = min(size, self.height())
        return QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)

    def _background_radius(self):
        return 5 if self.manager.theme.compact else 6


class StripeButton(_StripeEntryButton):
    """侧栏工具入口；所属侧栏管理生命周期，按钮本身不创建或持有业务 QWidget。"""

    def __init__(self, rail, tool_id):
        spec = rail.manager.specs[tool_id]
        tooltip = spec.title + (f"  {spec.shortcut}" if spec.shortcut else "")
        super().__init__(rail.manager, spec.icon, tooltip=tooltip, parent=rail)
        self.rail = rail
        self.tool_id = tool_id
        self.press = QPoint()
        self._dragged = False
        self.setObjectName(f"stripe_{tool_id}")
        self.setCheckable(True)
        self.clicked.connect(self._toggle)

    def _toggle(self):
        if not self._dragged:
            self.manager.toggle(self.tool_id)
        self._dragged = False

    def mousePressEvent(self, event):
        self.press = event.position().toPoint()
        self._dragged = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        distance = (event.position().toPoint() - self.press).manhattanLength()
        if event.buttons() & Qt.MouseButton.LeftButton and distance >= QApplication.startDragDistance():
            self._dragged = True
            self.setDown(False)
            self.manager.start_tool_drag(self.tool_id, self, self.press)

    def contextMenuEvent(self, event):
        self.manager.tool_menu(self.tool_id, self).exec(event.globalPos())


class StripeColumn(QWidget):
    """一侧的三个停靠组。host 拥有侧栏；所有操作须在 Qt 主线程执行。

    side 由 DockHost 传入 left 或 right。分组属于呈现层，不写入布局快照；
    工具位置、返回位置和排序仍以 DockManager 的 ToolState 为唯一来源。
    """

    def __init__(self, host, side):
        super().__init__(host)
        self.host = host
        self.manager = host.manager
        self.side = side
        self.groups = ([DockPosition.LEFT_TOP, DockPosition.LEFT_BOTTOM, DockPosition.BOTTOM_LEFT]
                       if side == "left" else
                       [DockPosition.RIGHT_TOP, DockPosition.RIGHT_BOTTOM, DockPosition.BOTTOM_RIGHT])
        self._buttons = {}
        self._layout_data = _StripeLayout()
        self._drag_tool_id = None
        self._insert_line = None
        self._drop_position = None
        self.more = _StripeEntryButton(self.manager, icon="more", tooltip="更多工具窗口", parent=self)
        self.more.clicked.connect(self.show_more)
        # 按钮采用统一几何定位，不让已注册工具的总高度参与宿主最小高度计算。
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Ignored)
        self.setAcceptDrops(True)
        self.manager.active_changed.connect(self._active_changed)

    def sizeHint(self):
        return QSize(self.width(), 0)

    def minimumSizeHint(self):
        return QSize(self.width(), 0)

    @staticmethod
    def _position(state):
        """驻留中央工作区的工具仍在最后停靠组保留返回入口。"""
        return state.last_position if state.position == "editor" else state.position

    def refresh(self):
        """从管理器同步入口与外观；复用按钮，不触发任何业务内容工厂。"""
        theme = self.manager.theme
        self.setFixedWidth(theme.rail_width)
        self.setVisible(self.manager.settings.show_bars)
        for tool_id in list(self._buttons):
            if tool_id not in self.manager.states:
                button = self._buttons.pop(tool_id)
                button.hide()
                button.deleteLater()
        self._relayout()

    def _calculate_layout(self):
        """先按优先级分配有限高度，再一次性生成可见按钮、组和落点矩形。

        活动工具优先，每个非空组其次获得代表入口，最后逐组轮流填充。
        空拖放组以临时占位参与同一高度预算；极小高度时允许没有工具按钮，
        但不会挤出更多入口、让按钮重叠或扩大宿主最小高度。
        """
        layout = _StripeLayout()
        states = sorted((state for state in self.manager.states.values()
                         if state.host_id == self.host.host_id and state.available
                         and self._position(state) in self.groups), key=lambda state: state.order)
        layout.members = {position: tuple(s.id for s in states if self._position(s) == position)
                          for position in self.groups}
        selected = {position: set() for position in self.groups}
        width, height = self.width(), self.height()
        button_height = max(1, self.manager.theme.button_height)
        separator_height = 8 if self.manager.theme.compact else 12
        # 更多入口与工具按钮占用等高整行；极小侧栏仅裁短该行，不撑高宿主。
        margin = min(4, max(0, height - button_height))
        more_height = min(button_height, max(0, height - margin))
        budget = max(0, height - margin - more_height)
        # 第一轮只分配真实入口，临时空组不能抢占活动工具和各组代表的预算。
        placeholder = 0

        def group_heights():
            return [len(selected[p]) * button_height or placeholder for p in self.groups]

        def select(position, tool_id):
            selected[position].add(tool_id)
            sizes = group_heights()
            cost = sum(sizes) + (separator_height if sizes[0] and sizes[1] else 0)
            if cost > budget:
                selected[position].remove(tool_id)

        active = self.manager.active_id
        for position, members in layout.members.items():
            if active in members:
                select(position, active)
        for position, members in layout.members.items():
            if members and not selected[position]:
                select(position, members[0])
        empty_groups = sum(not ids for ids in selected.values())
        if self._drag_tool_id is not None and empty_groups:
            # 有余量才展开空组；加入空组后上下两个侧边组可能同时出现，需预留分隔带。
            # 高度不足时缩小或收起占位，保证原本能够显示的活动入口不会在拖动时消失。
            occupied_height = sum(len(ids) * button_height for ids in selected.values())
            remaining_height = max(0, budget - occupied_height - separator_height)
            placeholder = min(button_height, remaining_height // empty_groups)
        # 已有代表入口不参与二次抢占；轮流取各组剩余成员，避免上组吃掉底部预算。
        remaining = {p: [tool_id for tool_id in members if tool_id not in selected[p]]
                     for p, members in layout.members.items()}
        for index in range(max((len(ids) for ids in remaining.values()), default=0)):
            for position in self.groups:
                if index < len(remaining[position]):
                    select(position, remaining[position][index])
        layout.visible = {p: tuple(tool_id for tool_id in members if tool_id in selected[p])
                          for p, members in layout.members.items()}
        sizes = group_heights()
        separator = separator_height if sizes[0] and sizes[1] else 0
        starts = [0, sizes[0] + separator, height - margin - sizes[2]]
        if separator:
            layout.separator = QRect(0, sizes[0], width, separator)
        for position, y, size in zip(self.groups, starts, sizes):
            layout.groups[position] = QRect(0, y, width, size)
            for index, tool_id in enumerate(layout.visible[position]):
                layout.buttons[tool_id] = QRect(0, y + index * button_height, width, button_height)
        layout.more = QRect(0, starts[1] + sizes[1], width, more_height)
        # 用相邻实际组边界的中线分配留白；分隔带也自然分成上下两半。
        occupied = [(p, rect) for p, rect in layout.groups.items() if not rect.isEmpty()]
        start = 0
        for index, (position, rect) in enumerate(occupied):
            end = ((rect.bottom() + 1 + occupied[index + 1][1].top()) // 2
                   if index + 1 < len(occupied) else height)
            layout.drop_areas[position] = QRect(0, start, width, end - start)
            start = end
        return layout

    def _relayout(self):
        """应用计算结果。鼠标移动只读快照；刷新、尺寸或活动工具变化才重新计算。"""
        self._layout_data = layout = self._calculate_layout()
        for tool_id, button in self._buttons.items():
            if tool_id not in layout.buttons:
                button.hide()
        for tool_id, rect in layout.buttons.items():
            button = self._buttons.get(tool_id)
            if button is None:
                button = self._buttons[tool_id] = StripeButton(self, tool_id)
            button.setFixedSize(rect.size())
            button.move(rect.topLeft())
            button.setChecked(self.manager.states[tool_id].visible)
            button.notification = self.manager.specs[tool_id].notification
            button.show()
            button.update()
        self.more.setFixedSize(layout.more.size())
        self.more.move(layout.more.topLeft())
        self.more.raise_()
        self.more.update()
        self.update()

    def _active_changed(self, tool_id):
        # 焦点变化只需重排侧栏，不能调用 manager.refresh 迁移所有业务控件。
        if self._drag_tool_id is None and not self.manager.refreshing:
            self._relayout()

    def _build_more_menu(self):
        """构建归侧栏所有的临时菜单；调用方使用后须 deleteLater。"""
        menu = QMenu(self)
        added = set()

        def add_tool(tool_id):
            state, spec = self.manager.states[tool_id], self.manager.specs[tool_id]
            action = menu.addAction(self.manager.theme.icon(spec.icon), spec.title)
            action.setData(tool_id)
            action.setCheckable(True)
            action.setChecked(state.visible)
            action.triggered.connect(lambda checked=False, selected=tool_id: self.manager.activate(selected))
            added.add(tool_id)

        for position, members in self._layout_data.members.items():
            overflow = [tool_id for tool_id in members if tool_id not in self._layout_data.buttons]
            if overflow:
                menu.addSection(POSITION_LABELS[position])
                for tool_id in overflow:
                    add_tool(tool_id)
        others = [s.id for s in self.manager.states.values() if s.available and s.id not in added]
        if added and others:
            menu.addSection("其他工具窗口")
        for tool_id in others:
            add_tool(tool_id)
        menu.addSeparator()
        action = menu.addAction("紧凑布局")
        action.setCheckable(True)
        action.setChecked(self.manager.settings.compact)
        action.triggered.connect(lambda checked: self.manager.set_option("compact", checked))
        return menu

    def show_more(self):
        """执行侧栏临时菜单；展开期间保留按下反馈，关闭或异常后统一释放。"""
        menu = self._build_more_menu()
        self.more.setDown(True)
        try:
            menu.exec(self.more.mapToGlobal(QPoint(0, self.more.height())))
        finally:
            self.more.setDown(False)
            menu.deleteLater()

    def contextMenuEvent(self, event):
        self.show_more()

    def resizeEvent(self, event):
        self._clear_preview()
        self._relayout()
        super().resizeEvent(event)

    def _insertion_index(self, position, anchor=None, after=False):
        """把显示按钮的前后关系换算为管理器完整目标组的索引。

        溢出及不可用成员仍占据原排序位置；中央工作区入口只充当相对位置锚点，
        不计入管理器的实际目标组。这里保留来源，提交时统一扣除同组移动的来源。
        """
        peers = sorted((s for s in self.manager.states.values() if s.host_id == self.host.host_id
                        and self._position(s) == position), key=lambda state: state.order)
        index = 0
        for state in peers:
            if state.id == anchor:
                return index + int(after and state.position == position)
            index += int(state.position == position)
        return index

    def drop_target(self, point):
        """返回侧栏局部 point 的 DockTarget；栏外、更多按钮或无落点时返回 None。

        方法仅更新插入提示，不改变管理器状态。调用方必须在有效工具拖放提交时
        调用 manager.move，并在取消或结束后 reset_drag 清理临时几何。
        """
        self._insert_line = None
        layout = self._layout_data
        if not self.rect().contains(point) or layout.more.contains(point):
            return None
        for position, area in layout.drop_areas.items():
            if not area.contains(point):
                continue
            for tool_id in layout.visible[position]:
                rect = layout.buttons[tool_id]
                if point.y() <= rect.bottom():
                    after = point.y() > rect.center().y()
                    self._insert_line = rect.bottom() if after else rect.top()
                    index = self._insertion_index(position, tool_id, after)
                    return DockTarget(position, self.host.host_id, index)
            rect = layout.groups[position]
            # 组末留白追加到完整组末尾；空占位的上半部则允许插入到完整组开头。
            before = not layout.visible[position] and point.y() <= rect.center().y()
            self._insert_line = rect.top() if before else rect.bottom()
            index = 0 if before else self._insertion_index(position)
            return DockTarget(position, self.host.host_id, index)
        return None

    def _tool_drag(self, mime):
        data = self.manager.decode_drag(mime)
        if data and data["kind"] == "tool" and self.manager.states[data["id"]].available:
            return data
        return None

    def _begin_drag(self, tool_id):
        if self._drag_tool_id != tool_id:
            self._drag_tool_id = tool_id
            self._relayout()

    def _clear_preview(self):
        if self._drop_position is not None:
            QToolTip.hideText()
        self._drop_position = None
        self._insert_line = None

    def reset_drag(self):
        """幂等结束侧栏拖放；可由离开、提交或管理器原生拖放 finally 调用。"""
        dragging = self._drag_tool_id is not None
        self._drag_tool_id = None
        self._clear_preview()
        if dragging:
            self._relayout()
        else:
            self.update()

    def dragEnterEvent(self, event):
        data = self._tool_drag(event.mimeData())
        if data:
            self._begin_drag(data["id"])
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        data = self._tool_drag(event.mimeData())
        if data:
            self._begin_drag(data["id"])
            point = event.position().toPoint()
            target = self.drop_target(point)
            if target is not None:
                if self._drop_position != target.position:
                    QToolTip.showText(self.mapToGlobal(point), POSITION_LABELS[target.position], self)
                self._drop_position = target.position
                self.update()
                event.acceptProposedAction()
                return
        self._clear_preview()
        self.update()
        event.ignore()

    def dragLeaveEvent(self, event):
        self.reset_drag()
        event.accept()

    def dropEvent(self, event):
        try:
            data = self._tool_drag(event.mimeData())
            if not data:
                event.ignore()
                return
            target = self.drop_target(event.position().toPoint())
            if target is None:
                event.ignore()
                return
            state = self.manager.states[data["id"]]
            if state.host_id == target.host_id and state.position == target.position:
                peers = sorted((s for s in self.manager.states.values()
                                if s.host_id == target.host_id and s.position == target.position),
                               key=lambda item: item.order)
                old_index = next(n for n, item in enumerate(peers) if item.id == state.id)
                if old_index < target.index:
                    target = DockTarget(target.position, target.host_id, target.index - 1)
            self.manager.move(data["id"], target)
            event.acceptProposedAction()
        finally:
            self.reset_drag()

    def paintEvent(self, event):
        painter = QPainter(self)
        theme = self.manager.theme
        separator = self._layout_data.separator
        if not separator.isEmpty():
            length = min(24, self.width())
            x = (self.width() - length) // 2
            painter.fillRect(QRect(x, separator.top() + separator.height() // 2, length, 1),
                             theme.color("tool-window-border"))
        if self._drag_tool_id is not None:
            painter.setPen(QPen(theme.color("tool-window-border"), 1, Qt.PenStyle.DashLine))
            for position, rect in self._layout_data.groups.items():
                if not self._layout_data.visible[position] and rect.width() > 7 and rect.height() > 7:
                    painter.drawRect(rect.adjusted(3, 3, -4, -4))
        if self._insert_line is not None:
            painter.setPen(theme.color("control-brand-border"))
            painter.drawLine(3, self._insert_line, self.width() - 3, self._insert_line)
