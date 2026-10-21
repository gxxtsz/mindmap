"""分支样式的原创图形预览、四列选择器和中文属性选项。"""

from functools import lru_cache

from mindmap_engine import BranchStyle, MindMapDocument, Node, Rect, Theme, route_branches
from PySide6.QtCore import QEvent, QRectF, QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QLabel,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from .rendering import paint_branch
from .ui_style import ComboBox, apply_ui_palette, control_styles, menu_styles, ui_colors

# 透明留白同时用于阴影和入口右侧对齐，调整弹层尺寸时两处必须保持一致。
BRANCH_POPUP_PADDING = 6


@lru_cache(maxsize=256)
def _style_icon(kind, foreground, disabled, accent, border) -> QIcon:
    """图标也走正式路由，按主题和多档物理分辨率缓存，避免预览与实际连线漂移。"""
    icon = QIcon()
    for color, mode, state in (
        (foreground, QIcon.Mode.Normal, QIcon.State.Off),
        (disabled, QIcon.Mode.Disabled, QIcon.State.Off),
        (accent, QIcon.Mode.Normal, QIcon.State.On),
        (accent, QIcon.Mode.Active, QIcon.State.Off),
    ):
        document = MindMapDocument.new()
        # 图标必须按请求类型独立路由，None 表示原始曲线，不沿用新文档初始样式。
        document.root.style.clear()
        document.theme.palette = [color]
        if kind is not None:
            document.root.style["branch"] = {"kind": kind}
        nodes = {document.root_id: Rect(2, 26, 14, 12)}
        for y in (5, 27, 49):
            child = Node("")
            document.root.children.append(child.id)
            document.nodes[child.id] = child
            nodes[child.id] = Rect(47, y, 14, 10)
        branches = route_branches(document, nodes)
        for ratio in (1.0, 1.5, 2.0, 3.0):
            pixmap = QPixmap(round(64 * ratio), round(64 * ratio))
            pixmap.fill(Qt.GlobalColor.transparent)
            pixmap.setDevicePixelRatio(ratio)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            for branch in branches.values():
                paint_branch(painter, branch)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(border))
            for rect in nodes.values():
                painter.drawRoundedRect(QRectF(rect.x, rect.y, rect.width, rect.height), 2, 2)
            painter.end()
            icon.addPixmap(pixmap, mode, state)
    return icon


def branch_style_icon(kind: BranchStyle | str | None, theme: Theme) -> QIcon:
    """返回可复用于动作和属性下拉框的多分辨率图标，原始曲线也有独立预览。"""
    colors = ui_colors(theme)
    return _style_icon(kind, colors.text, colors.muted, colors.accent, colors.border)


def populate_branch_combo(combo: QComboBox, theme: Theme) -> None:
    """填充中文样式选项；提交时机由属性弹窗或侧栏控制器决定。"""
    combo.addItem(branch_style_icon(None, theme), "原始曲线", None)
    for kind in BranchStyle:
        combo.addItem(branch_style_icon(kind, theme), kind.label, kind.value)


class _StyleButton(QToolButton):
    entered = Signal()
    left = Signal()

    def enterEvent(self, event):
        super().enterEvent(event)
        self.entered.emit()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.left.emit()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.entered.emit()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.left.emit()


class BranchStyleMenu(QMenu):
    """四列十一项网格；自己不访问引擎、不持有正式文档。

    compact 用于已有外部范围控件的属性入口，只显示圆角图形网格和原始曲线；
    工具栏保留完整标题、范围及候选预览信号。宿主分别决定自动提交或暂存至确定。
    """

    preview_requested = Signal(object)
    preview_finished = Signal()
    style_selected = Signal(object)
    scope_changed = Signal(str)

    def __init__(self, parent=None, *, compact=False):
        super().__init__(parent)
        self.compact = compact
        self.setAccessibleName("分支连接线样式")
        content = QWidget(self)
        content.setObjectName("branch_palette_panel")
        content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        outer = QVBoxLayout(content)
        outer.setContentsMargins(10, 8, 10, 8)
        if compact:
            # 四列按钮、三行网格和底部恢复入口合计约 268×230 逻辑像素。
            # 只收紧展开弹层，属性表单中入口按钮的尺寸和交互保持原样。
            outer.setSpacing(4)
        self.summary = QLabel("原始曲线", content)
        outer.addWidget(self.summary)
        self.scope = None
        if compact:
            # 属性面板已有作用范围；紧凑弹层只呈现网格，避免重复标题和范围下拉框。
            self.summary.hide()
            self.setObjectName("branch_grid_popup")
            self.setWindowFlags(
                self.windowFlags() | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            shadow = QGraphicsDropShadowEffect(content)
            # 阴影外扩范围不超过透明留白，避免缩小后在菜单边缘产生截断。
            shadow.setBlurRadius(5)
            shadow.setOffset(0, 1)
            shadow.setColor(QColor(0, 0, 0, 42))
            content.setGraphicsEffect(shadow)
        else:
            self.scope = ComboBox(content)
            self.scope.setObjectName("branch_scope")
            self.scope.setAccessibleName("分支样式作用范围")
            self.scope.addItem("整个子树", "subtree")
            self.scope.addItem("仅本级", "level")
            self.scope.currentIndexChanged.connect(lambda: self.scope_changed.emit(self.scope.currentData()))
            outer.addWidget(self.scope)
        grid = QGridLayout()
        grid.setSpacing(4 if compact else 5)
        self.grid = grid
        outer.addLayout(grid)
        self.buttons = {}
        for index, kind in enumerate(BranchStyle):
            button = _StyleButton(content)
            button.setObjectName(f"branch_{kind.value}")
            button.setProperty("branch_style_option", True)
            button.setCheckable(True)
            button.setFixedSize(56, 54) if compact else button.setFixedSize(68, 62)
            button.setIconSize(QSize(40, 40) if compact else QSize(54, 54))
            button.setToolTip(kind.label)
            button.setAccessibleName(kind.label)
            button.installEventFilter(self)
            button.entered.connect(lambda value=kind: self.preview_requested.emit(value))
            button.left.connect(self.preview_finished.emit)
            button.clicked.connect(lambda checked=False, value=kind: self.style_selected.emit(value))
            grid.addWidget(button, index // 4, index % 4)
            self.buttons[kind] = button
        self.reset = _StyleButton(content)
        self.reset.setText("恢复原始曲线")
        self.reset.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.reset.installEventFilter(self)
        if compact:
            self.reset.setObjectName("branch_palette_reset")
            self.reset.setCheckable(True)
            self.reset.setFixedHeight(28)
            self.reset.setIconSize(QSize(18, 18))
        self.reset.entered.connect(lambda: self.preview_requested.emit(None))
        self.reset.left.connect(self.preview_finished.emit)
        self.reset.clicked.connect(lambda: self.style_selected.emit(None))
        outer.addWidget(self.reset)
        action = QWidgetAction(self)
        action.setDefaultWidget(content)
        self.addAction(action)
        self.aboutToHide.connect(self.preview_finished.emit)

    def sync(self, kinds: set[BranchStyle | None], scope: str, theme: Theme) -> None:
        """根据正式选择同步混合状态、图标和主题，悬停不改变真实勾选项。"""
        colors = ui_colors(theme)
        signature = colors
        if self.property("branch_theme") != signature:
            # QWidgetAction 中的标签和原生组合框不会自动采用工具栏样式表的文字颜色；
            # 显式同步两者，避免深色弹层仍出现黑字及浅色输入背景。只在主题变化时重设。
            self.setStyleSheet(menu_styles(colors) + control_styles(colors))
            if self.compact:
                # 阴影留在透明菜单边缘，选中态用浅底圆角，图标保持与截图一致的普通描边。
                self.setStyleSheet(
                    f"QMenu#branch_grid_popup {{ background: transparent; border: none; padding: {BRANCH_POPUP_PADDING}px; }}"
                    f"QWidget#branch_palette_panel {{ background: {colors.surface}; border: 1px solid {colors.border}; border-radius: 8px; }}"
                    f"QToolButton {{ color: {colors.text}; background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 2px; }}"
                    f"QToolButton:hover, QToolButton:checked {{ background: {colors.hover}; }}"
                    f"QToolButton:focus {{ background: {colors.hover}; border-color: {colors.accent}; }}"
                )
            apply_ui_palette(self, colors)
            if self.scope is not None:
                apply_ui_palette(self.scope, colors)
                apply_ui_palette(self.scope.view(), colors)
            self.setProperty("branch_theme", signature)
        mixed = len(kinds) != 1
        current = next(iter(kinds)) if not mixed else None
        self.summary.setText("多种样式" if mixed else current.label if current else "原始曲线")
        self.setAccessibleDescription(self.summary.text())
        if self.scope is not None:
            with QSignalBlocker(self.scope):
                self.scope.setCurrentIndex(max(0, self.scope.findData(scope)))
        for kind, button in self.buttons.items():
            button.setIcon(
                _style_icon(kind, colors.text, colors.muted, colors.text, colors.border)
                if self.compact
                else branch_style_icon(kind, theme)
            )
            button.setChecked(not mixed and kind == current)
        self.reset.setIcon(branch_style_icon(None, theme))
        if self.compact:
            self.reset.setChecked(not mixed and current is None)

    def eventFilter(self, watched, event):
        """按四列布局处理方向键，聚焦沿用悬停预览，Escape 由菜单统一取消。"""
        if event.type() == QEvent.Type.KeyPress and (
            watched in self.buttons.values() or watched is self.reset
        ):
            if event.key() == Qt.Key.Key_Escape:
                self.close()
                return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
                watched.click()
                return True
            if watched is self.reset:
                if event.key() == Qt.Key.Key_Up:
                    list(self.buttons.values())[-1].setFocus(Qt.FocusReason.OtherFocusReason)
                    return True
                return super().eventFilter(watched, event)
            offsets = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_Up: -4, Qt.Key.Key_Down: 4}
            if event.key() in offsets:
                buttons = list(self.buttons.values())
                index = buttons.index(watched) + offsets[event.key()]
                if 0 <= index < len(buttons):
                    buttons[index].setFocus(Qt.FocusReason.OtherFocusReason)
                return True
        return super().eventFilter(watched, event)
