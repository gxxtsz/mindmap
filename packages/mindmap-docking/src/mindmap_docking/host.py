"""六区停靠宿主：左右纵向区、底部双区、可嵌套编辑区和覆盖层。"""

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget

from .controls import DockSplitter, DropOverlay
from .editors import EditorArea
from .models import POSITION_LABELS, DockPosition, DockTarget, ViewMode, default_region_sizes
from .panels import OverlayContainer
from .stripes import StripeColumn


class DockHost(QWidget):
    """可嵌入宿主窗口的公共控件。

    每个管理器只允许一个 host_id='main' 的宿主；非法标识在注册前拒绝，
    面板和编辑器分屏始终归属同一主窗口。
    """

    def __init__(self, manager, host_id="main", parent=None):
        super().__init__(parent)
        if host_id != "main":
            raise ValueError("仅支持 main 主宿主")
        if host_id in manager.hosts:
            raise ValueError(f"宿主标识重复：{host_id}")
        self.manager = manager
        self.host_id = host_id
        self.region_sizes = default_region_sizes()
        self._applying_sizes = False
        self._sizes_pending = False
        self.setObjectName(f"dock_host_{host_id}")
        self.setAcceptDrops(True)
        self.manager.hosts[host_id] = self
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self.left_rail = StripeColumn(self, "left")
        self.right_rail = StripeColumn(self, "right")
        self.canvas = QWidget(self)
        self.canvas_layout = QVBoxLayout(self.canvas)
        self.canvas_layout.setContentsMargins(0, 0, 0, 0)
        self.canvas_layout.setSpacing(0)
        self._layout.addWidget(self.left_rail)
        self._layout.addWidget(self.canvas, 1)
        self._layout.addWidget(self.right_rail)
        self.horizontal = DockSplitter(Qt.Orientation.Horizontal, manager)
        self.vertical = DockSplitter(Qt.Orientation.Vertical, manager)
        self.left = DockSplitter(Qt.Orientation.Vertical, manager)
        self.right = DockSplitter(Qt.Orientation.Vertical, manager)
        self.bottom = DockSplitter(Qt.Orientation.Horizontal, manager)
        self.zones = {}
        for position in DockPosition:
            stack = QStackedWidget()
            stack.setObjectName(f"zone_{host_id}_{position}")
            stack.setMinimumSize(120, 80)
            self.zones[position] = stack
            if position.startswith("left"):
                self.left.addWidget(stack)
            elif position.startswith("right"):
                self.right.addWidget(stack)
            else:
                self.bottom.addWidget(stack)
        self.editor = EditorArea(self)
        self.workspace = self.editor
        self.editor.setMinimumSize(220, 140)
        self.overlays = {}
        self.preview = DropOverlay(self)
        self._wide = None
        self._sizes = {"horizontal": [285, 900, 285], "vertical": [700, 260], "left": [600, 400], "right": [600, 400], "bottom": [500, 500]}
        self._rebuild_structure()
        for name in self._sizes:
            splitter = getattr(self, name)
            splitter.splitterMoved.connect(
                lambda _, index, selected=name: self._splitter_moved(selected, index))
        for widget in (self.canvas, self.horizontal, self.vertical, self.left,
                       self.right, self.bottom, self.editor):
            widget.installEventFilter(self)
        manager.install_window_actions(self)

    def _rebuild_structure(self):
        wide = self.manager.settings.widescreen
        if self._wide == wide:
            return
        # 先拆掉相互嵌套关系，再重新装配；只迁移容器，不重建任何工具内容。
        for widget in [self.left, self.right, self.bottom, self.editor, self.horizontal, self.vertical]:
            widget.hide()
            widget.setParent(None)
        if wide:
            self.vertical.addWidget(self.editor)
            self.vertical.addWidget(self.bottom)
            self.horizontal.addWidget(self.left)
            self.horizontal.addWidget(self.vertical)
            self.horizontal.addWidget(self.right)
            self.canvas_layout.addWidget(self.horizontal)
        else:
            self.horizontal.addWidget(self.left)
            self.horizontal.addWidget(self.editor)
            self.horizontal.addWidget(self.right)
            self.vertical.addWidget(self.horizontal)
            self.vertical.addWidget(self.bottom)
            self.canvas_layout.addWidget(self.vertical)
        self._wide = wide
        for widget in [self.horizontal, self.vertical, self.editor]:
            widget.show()

    def _splitter_moved(self, name=None, index=None):
        """只记录用户实际拖动的区域或内部比例，不采集隐藏区的零尺寸。

        name/index 由对应 QSplitter 信号提供；省略参数时测量所有可见区域，
        供测试及宿主内部显式调整后的同步使用。程序布置期间不记忆尺寸。
        """
        if self.manager.refreshing or self._applying_sizes:
            return
        if name in (None, "horizontal"):
            for region, handle in [("left", 1), ("right", 2)]:
                widget = getattr(self, region)
                if widget.isVisible() and (index is None or index == handle):
                    self.region_sizes["dock"][region] = max(1, widget.width())
        if name in (None, "vertical") and self.bottom.isVisible():
            self.region_sizes["dock"]["bottom"] = max(1, self.bottom.height())
        for selected in (self._sizes if name is None else [name]):
            splitter = getattr(self, selected)
            values = splitter.sizes()
            if selected in ("horizontal", "vertical"):
                # 保留旧字段以兼容旧格式；外部区域的恢复只读取 region_sizes。
                self._sizes[selected] = [value or previous for value, previous
                                         in zip(values, self._sizes[selected])]
            elif values and all(value > 0 for value in values):
                self._sizes[selected] = values
        self.manager.schedule_save()

    def _set_region_extent(self, position, value, *, overlay=False):
        """修改当前宿主的共享尺寸并立即布置，不持有任何工具控件。

        position 必须是六个停靠位置之一，非法位置抛出 ValueError；value 是内部
        拖动或快捷键计算的逻辑像素值。布局文件另行完整预检，不经此方法读取。
        """
        region = DockPosition(position).value.split("_")[0]
        minimum = 140 if overlay else (80 if region == "bottom" else 120)
        self.region_sizes["overlay" if overlay else "dock"][region] = min(1000000, max(minimum, value))
        if overlay:
            self.position_overlays()
        else:
            self._apply_sizes()
        self.manager.schedule_save()

    def clear_panels(self):
        for stack in self.zones.values():
            while stack.count():
                stack.removeWidget(stack.widget(0))
        for group in self.editor.groups:
            for index in reversed(range(group.count())):
                token = group.tabBar().tabData(index)
                if isinstance(token, str) and token.startswith("tool:"):
                    group.removeTab(index)
        for overlay in self.overlays.values():
            while overlay._layout.count():
                overlay._layout.takeAt(0)
            overlay.hide()

    def refresh(self):
        manager = self.manager
        self._rebuild_structure()
        self.refresh_theme()
        self.left.setOrientation(Qt.Orientation.Horizontal if manager.settings.left_side_by_side else Qt.Orientation.Vertical)
        self.right.setOrientation(Qt.Orientation.Horizontal if manager.settings.right_side_by_side else Qt.Orientation.Vertical)
        self._refresh_contents()

    def refresh_theme(self):
        """只刷新外观和尺寸，不移除业务控件，允许宿主保留正在输入的草稿。"""
        theme = self.manager.theme
        # 两侧与底部边距继续跟随主题，顶部单独收至 2 个逻辑像素，
        # 让工具面板和文档标签紧接标题栏；不改变分屏内部间距及标签的归属。
        outer = max(2, theme.gap - 2)
        self.canvas_layout.setContentsMargins(outer, 2, outer, theme.gap)
        theme.apply(self, f"QWidget#{self.objectName()} {{background:{theme.css('main-window-bg')};}} QWidget#editor_{self.host_id} {{background:{theme.css('main-window-bg')};}}")
        self.editor.refresh_theme()
        for splitter in [self.horizontal, self.vertical, self.left, self.right, self.bottom]:
            splitter.refresh_theme()
        self.left_rail.refresh()
        self.right_rail.refresh()
        self._apply_sizes()
        self.position_overlays()

    def _refresh_contents(self):
        manager = self.manager
        theme = manager.theme
        active_positions = set()
        for state in manager.states.values():
            if state.host_id != self.host_id or not state.visible or not manager.is_tool_available(state.id):
                continue
            panel = manager.panel(state.id)
            panel.refresh_theme()
            if state.position == "editor":
                spec = manager.specs[state.id]
                group = self.editor.group(state.editor_group)
                group.add_token("tool:" + state.id, panel, spec.title, theme.icon(spec.icon, 16), state.order)
                continue
            if state.mode == ViewMode.UNDOCK:
                overlay = self.overlays.get(state.id)
                if overlay is None:
                    overlay = self.overlays[state.id] = OverlayContainer(self, state.id)
                overlay._layout.addWidget(panel)
                panel.show()
                overlay.show()
                continue
            stack = self.zones[DockPosition(state.position)]
            stack.addWidget(panel)
            stack.setCurrentWidget(panel)
            panel.show()
            active_positions.add(DockPosition(state.position))
        for position, stack in self.zones.items():
            stack.setVisible(position in active_positions)
        self.left.setVisible(any(position.startswith("left") for position in active_positions))
        self.right.setVisible(any(position.startswith("right") for position in active_positions))
        self.bottom.setVisible(any(position.startswith("bottom") for position in active_positions))
        self.editor.setVisible(True)
        # 构造时 QSplitter 尚未取得尺寸，延迟一帧再恢复比例，避免初次显示宽度为零。
        self.left_rail.refresh()
        self.right_rail.refresh()
        self._apply_sizes()
        self.position_overlays()
        self.preview.raise_()
        self._schedule_sizes()

    def _apply_sizes(self):
        """以区域的目标像素尺寸布置外部边界，中央区域吸收可用空间差值。

        QSplitter 负责最小尺寸限幅，压缩后的实际尺寸不能反写目标值，因此窗口
        恢复空间后仍能回到用户调整的尺寸。先布置父分割器，再布置其内部子分割器。
        """
        if self._applying_sizes:
            return
        self._applying_sizes = True
        try:
            self._layout.activate()
            self.canvas_layout.activate()

            def available(splitter, axis):
                count = sum(not splitter.widget(n).isHidden() for n in range(splitter.count()))
                return max(0, getattr(splitter, axis)() - max(0, count - 1) * splitter.handleWidth())

            def distribute(splitter, values, center, minimum):
                splitter.setSizes(values)
                # 区域刚显示时 Qt 的分割手柄可能尚未完成显隐更新。用实际总空间校正
                # 中央余量，避免第一次打开工具时连边缘尺寸也被按比例放大或缩小。
                total = sum(splitter.sizes())
                if values[center] and total != sum(values):
                    edges = sum(value for index, value in enumerate(values) if index != center)
                    values[center] = max(minimum, total - edges)
                    splitter.setSizes(values)

            def horizontal():
                sizes = self.region_sizes["dock"]
                left = 0 if self.left.isHidden() else sizes["left"]
                right = 0 if self.right.isHidden() else sizes["right"]
                center = (0 if self.horizontal.widget(1).isHidden() else
                          max(220, available(self.horizontal, "width") - left - right))
                distribute(self.horizontal, [left, center, right], 1, 220)

            def vertical():
                bottom = 0 if self.bottom.isHidden() else self.region_sizes["dock"]["bottom"]
                center = (0 if self.vertical.widget(0).isHidden() else
                          max(140, available(self.vertical, "height") - bottom))
                distribute(self.vertical, [center, bottom], 0, 140)

            for apply in ([horizontal, vertical] if self._wide else [vertical, horizontal]):
                apply()
            for name in ("left", "right", "bottom"):
                getattr(self, name).setSizes(self._sizes[name])
        finally:
            self._applying_sizes = False

    def _schedule_sizes(self):
        """合并窗口缩放后的布置，等待 Qt 完成父子布局后再恢复共享尺寸。"""
        if not self._sizes_pending and not self.manager.closing:
            self._sizes_pending = True
            QTimer.singleShot(0, self._apply_deferred_sizes)

    def _apply_deferred_sizes(self):
        self._sizes_pending = False
        if self.manager.closing or self.manager.hosts.get(self.host_id) is not self:
            return
        self._apply_sizes()
        self.position_overlays()

    def eventFilter(self, watched, event):
        # 初次显示及恢复布局时，手柄的延迟显隐会改变子区域几何，但不一定再次
        # 改变宿主本身。监听局部容器的实际 Resize，并跳过自己布置触发的事件，
        # 使最后一轮 Qt 布局之后仍能落实目标尺寸，不依赖固定次数的延迟重试。
        if event.type() == QEvent.Type.Resize and not self._applying_sizes:
            self._schedule_sizes()
        return super().eventFilter(watched, event)

    def position_overlays(self):
        if not hasattr(self, "overlays"):
            return
        area = QRect(self.canvas.mapTo(self, QPoint()), self.canvas.size()).adjusted(0, 0, 0, -self.manager.theme.gap)
        for tool_id, overlay in self.overlays.items():
            state = self.manager.states[tool_id]
            if not overlay.isVisible() or not state.visible:
                continue
            position = state.position
            extent = max(140, self.region_sizes["overlay"][position.split("_")[0]])
            if position.startswith("bottom"):
                extent = min(extent, max(140, area.height() - 50))
                rect = QRect(area.left(), area.bottom() - extent + 1, area.width(), extent)
            else:
                extent = min(extent, max(140, area.width() - 80))
                x = area.left() if position.startswith("left") else area.right() - extent + 1
                rect = QRect(x, area.top(), extent, area.height())
            overlay.setGeometry(rect)
            overlay.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.position_overlays()
        if hasattr(self, "preview"):
            self.preview.setGeometry(self.rect())
        if hasattr(self, "_sizes"):
            self._schedule_sizes()

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule_sizes()

    def drop_target(self, point):
        canvas = QRect(self.canvas.mapTo(self, QPoint()), self.canvas.size())
        x, y = point.x() - canvas.left(), point.y() - canvas.top()
        w, h = canvas.width(), canvas.height()
        if x < w * 0.22:
            position = DockPosition.LEFT_TOP if y < h / 2 else DockPosition.LEFT_BOTTOM
            preview = QRect(canvas.left(), canvas.top() if y < h / 2 else canvas.top() + h // 2, max(160, w // 4), h // 2)
        elif x > w * 0.78:
            position = DockPosition.RIGHT_TOP if y < h / 2 else DockPosition.RIGHT_BOTTOM
            preview = QRect(canvas.right() - w // 4, canvas.top() if y < h / 2 else canvas.top() + h // 2, w // 4, h // 2)
        elif y > h * 0.72:
            position = DockPosition.BOTTOM_LEFT if x < w / 2 else DockPosition.BOTTOM_RIGHT
            preview = QRect(canvas.left() if x < w / 2 else canvas.left() + w // 2, canvas.top() + h * 3 // 4, w // 2, h // 4)
        else:
            return DockTarget("editor", self.host_id), canvas.adjusted(w // 4, h // 5, -w // 4, -h // 5), "作为编辑器标签"
        return DockTarget(position, self.host_id), preview, "停靠到" + POSITION_LABELS[position]

    def dragEnterEvent(self, event):
        if self.manager.decode_drag(event.mimeData()):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if self.manager.decode_drag(event.mimeData()):
            target, rect, caption = self.drop_target(event.position().toPoint())
            self.preview.present(rect, caption)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self.preview.hide()

    def dropEvent(self, event):
        data = self.manager.decode_drag(event.mimeData())
        self.preview.hide()
        if not data:
            return
        target, _, _ = self.drop_target(event.position().toPoint())
        if data["kind"] == "tool":
            self.manager.move(data["id"], target)
        else:
            self.manager.move_document(data["id"], self.host_id)
        event.acceptProposedAction()
