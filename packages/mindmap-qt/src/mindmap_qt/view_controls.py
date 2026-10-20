"""画布右下角的轻量视图入口；使用普通子控件，不进入场景和图形导出。"""

from PySide6.QtCore import QEvent, QPoint, QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QHBoxLayout, QToolButton, QWidget

from .ui_style import UiColors


class _ViewControls(QWidget):
    """在视口内部固定显示视图动作，父视图负责控件及事件过滤器的生命周期。

    actions 为画布已有的 QAction，保留其所有权、快捷键和启用状态。本控件
    不占用画布布局空间，也不受场景平移和缩放影响；所有尺寸均为逻辑像素。
    """

    def __init__(self, view, actions):
        # QGraphicsView 平移时会滚动 viewport 的子控件，因此浮条归属外层视图，
        # 仅以 viewport 的几何区域定位，防止缩放或平移时被一起带走。
        super().__init__(view)
        self._viewport = view.viewport()
        self.setObjectName("mindmap_view_controls")
        self.setAccessibleName("画布视图操作")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        # 浮条空隙的点击不继续传给视口，避免意外取消选择或开始画布拖动。
        self.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(3)
        for action in actions:
            button = QToolButton(self)
            button.setDefaultAction(action)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            button.setIconSize(QSize(20, 20))
            button.setFixedSize(32, 32)
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.setAccessibleName(action.text())
            button.setAccessibleDescription(action.toolTip())
            layout.addWidget(button)
        self._shadow = QGraphicsDropShadowEffect(self)
        self._shadow.setBlurRadius(14)
        self._shadow.setOffset(0, 2)
        self.setGraphicsEffect(self._shadow)
        self._colors = None
        self.setFixedSize(self.sizeHint())
        self._viewport.installEventFilter(self)
        self._reposition()

    def set_ui_colors(self, colors: UiColors) -> None:
        """仅刷新浮条表面与交互色；图标通过原 QAction 自动跟随画布色板。"""
        if colors == self._colors:
            return
        self._colors = colors
        self.setStyleSheet(
            f"QWidget#mindmap_view_controls {{ background: {colors.surface}; "
            f"border: 1px solid {colors.border}; border-radius: 9px; }}"
            "QWidget#mindmap_view_controls QToolButton { background: transparent; "
            f"color: {colors.muted}; border: 1px solid transparent; border-radius: 5px; }}"
            f"QWidget#mindmap_view_controls QToolButton:hover {{ background: {colors.hover}; }}"
            "QWidget#mindmap_view_controls QToolButton:pressed { "
            f"background: {colors.selected}; }}"
            "QWidget#mindmap_view_controls QToolButton:focus { "
            f"border-color: {colors.accent}; }}"
        )
        shadow = QColor(colors.text)
        shadow.setAlpha(24)
        self._shadow.setColor(shadow)

    def _reposition(self):
        """随可见视口调整锚点，保留 16 像素边距，并防止窄视口产生负坐标。"""
        viewport = self._viewport
        position = QPoint(max(0, viewport.width() - self.width() - 16),
                          max(0, viewport.height() - self.height() - 16))
        self.move(viewport.mapTo(self.parentWidget(), position))
        self.raise_()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Move, QEvent.Type.Show):
            self._reposition()
        return super().eventFilter(watched, event)
