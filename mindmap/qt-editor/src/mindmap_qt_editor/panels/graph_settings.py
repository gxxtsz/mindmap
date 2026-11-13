"""活动图页的停靠设置，由画布动作负责校验权限和修改文档。"""

from typing import Protocol

from mindmap_core import NodeGraph
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QToolButton, QVBoxLayout, QWidget

from mindmap_qt_editor.theme import UiColors, apply_ui_palette, control_styles


class _GraphSettingsHost(Protocol):
    """图页面设置使用的宿主接口，由活动画布在 GUI 线程提供。"""

    @property
    def graph(self) -> NodeGraph | None:
        """返回当前图页的只读快照；树页返回 None，面板仅用于判断是否绑定动作。"""
        ...

    def action(self, action_id: str) -> QAction:
        """按标识借用宿主持有的动作，按钮复用该动作的文字、启用状态和触发行为。"""
        ...


class GraphSettingsPanel(QWidget):
    """右侧图页设置面板，每个窗口复用一份控件，操作始终交给当前活动图页。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("graph_settings_panel")
        self.setAccessibleName("图页面设置")
        self.setAutoFillBackground(True)
        self.layout_button = QToolButton(self)
        self.layout_button.setObjectName("graph_auto_layout_button")
        self.layout_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.layout_button.setEnabled(False)
        self.setFocusProxy(self.layout_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        layout.addWidget(self.layout_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()

    def bind_canvas(self, canvas: _GraphSettingsHost | None) -> None:
        """绑定活动图页的 DOT 动作，供按钮复用菜单的文字、提示及启用状态。

        传入 None 或树页时解绑。切换画布先移除旧动作，重复绑定同一动作不产生
        新连接。动作仍由原画布持有，本方法只更新控件，不触发布局或保存。
        """
        action = canvas.action("graph_auto_layout") if canvas is not None and canvas.graph is not None else None
        button = self.layout_button
        previous = button.defaultAction()
        if action is previous:
            return
        if previous is not None:
            button.setDefaultAction(None)
            button.removeAction(previous)
        if action is not None:
            button.setDefaultAction(action)
            button.setAccessibleName(action.text())
            button.setAccessibleDescription(action.toolTip())
        button.setEnabled(action is not None and action.isEnabled())

    def set_ui_colors(self, colors: UiColors) -> None:
        """按窗口色板更新侧栏和按钮外观，保留当前动作、焦点和布局状态。"""
        apply_ui_palette(self, colors, window=colors.surface)
        self.setStyleSheet(control_styles(colors))
