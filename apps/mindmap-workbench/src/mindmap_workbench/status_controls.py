"""状态栏右侧的画布操作：复用活动文档动作，不持有导图内容或文件状态。"""

from mindmap_qt_canvas import MindMapWidget
from mindmap_qt_ui import UiColors, toolbar_icon
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QHBoxLayout, QToolButton, QWidget


class _StatusViewControls(QWidget):
    """由主窗口持有的一组常驻按钮，切换文档时只替换动作及状态订阅。

    适应窗口和回到中心直接借用画布动作，快捷键仍由原画布注册；滚轮按钮
    独立调用公开偏好接口，避免借用带菜单的动作后出现下拉箭头或弹出入口。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("mindmap_status_controls")
        self.setAccessibleName("画布视图操作")
        self._canvas = None
        self._bound_actions = []
        self._colors = None
        self.buttons = {}
        self._labels = {"fit": "适应窗口", "center": "回到中心", "wheel_mode": "滚轮行为"}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(2)
        for key in self._labels:
            button = QToolButton(self)
            button.setObjectName("status_" + key)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            # 鼠标点击不移走画布或正文编辑器焦点；键盘仍可通过 Tab 到达按钮。
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.setFixedSize(24, 20)
            button.setIconSize(QSize(16, 16))
            layout.addWidget(button)
            self.buttons[key] = button
        self.buttons["wheel_mode"].clicked.connect(self._toggle_wheel_mode)
        self._sync_actions()

    def bind_canvas(self, canvas: MindMapWidget | None) -> None:
        """绑定当前活动画布；传入 None 禁用入口，并解除对旧会话的全部引用。

        原动作属于会话，可能在关闭标签后被延迟销毁。必须先断开信号、移除
        按钮关联动作，再接入新画布，防止后台标签刷新或销毁后访问旧 Qt 对象。
        重复绑定同一画布不追加订阅，也不改写任何偏好或文档状态。
        """
        if canvas is self._canvas:
            return
        for action in self._bound_actions:
            action.changed.disconnect(self._sync_actions)
        self._bound_actions.clear()
        for key in ("fit", "center"):
            button = self.buttons[key]
            previous = button.defaultAction()
            if previous is not None:
                button.setDefaultAction(None)
                button.removeAction(previous)
        self._canvas = canvas
        if canvas is not None:
            for key, button in self.buttons.items():
                action = canvas.action(key)
                if key != "wheel_mode":
                    button.setDefaultAction(action)
                # 滚轮动作在模式和主题变化时都会发出 changed，统一同步图标和提示；
                # 视图动作还借此同步编辑期间的禁用状态及自定义快捷键说明。
                action.changed.connect(self._sync_actions)
                self._bound_actions.append(action)
        self._sync_actions()

    def _sync_actions(self):
        """只刷新展示状态，不反向触发动作；没有导图时保持按钮位置及无障碍名称。"""
        for key, button in self.buttons.items():
            label = self._labels[key]
            if self._canvas is None:
                button.setEnabled(False)
                description = f"{label}（没有打开的导图）"
            else:
                action = self._canvas.action(key)
                button.setEnabled(action.isEnabled())
                if key == "wheel_mode":
                    mode = self._canvas.wheel_mode
                    current, following = ("上下滚动", "缩放") if mode == "scroll" else ("缩放", "上下滚动")
                    label = f"滚轮行为：{current}"
                    description = f"{label}；点击切换为{following}，Ctrl＋滚轮始终缩放"
                    button.setIcon(action.icon())
                else:
                    shortcuts = " / ".join(
                        key.toString(QKeySequence.SequenceFormat.NativeText) for key in action.shortcuts()
                    )
                    description = f"{action.toolTip()}（{shortcuts}）" if shortcuts else action.toolTip()
            button.setToolTip(description)
            button.setAccessibleName(label)
            button.setAccessibleDescription(description)

    def _toggle_wheel_mode(self):
        """每次点击只在两种模式间切换；持久化和跨标签同步交给宿主现有信号链。"""
        if self._canvas is not None:
            self._canvas.set_wheel_mode("zoom" if self._canvas.wheel_mode == "scroll" else "scroll")

    def set_ui_colors(self, colors: UiColors, *, compact: bool) -> None:
        """使用外壳色板和逻辑像素尺寸，不撑高原有的普通／紧凑状态栏。

        滚轮模式仅通过图标区分，按钮始终不可勾选；悬停、按下和键盘焦点仍有
        短暂反馈。无文档时也提供主题一致的禁用图标，不创建临时画布。
        """
        self._colors = colors
        self.setStyleSheet(
            "QWidget#mindmap_status_controls { background:transparent; }"
            "QWidget#mindmap_status_controls QToolButton { background:transparent; padding:0; "
            "border:1px solid transparent; border-radius:4px; }"
            f"QWidget#mindmap_status_controls QToolButton:hover {{ background:{colors.hover}; }}"
            f"QWidget#mindmap_status_controls QToolButton:pressed {{ background:{colors.selected}; }}"
            f"QWidget#mindmap_status_controls QToolButton:focus {{ border-color:{colors.accent}; }}"
        )
        for key, button in self.buttons.items():
            button.setFixedSize(22 if compact else 24, 18 if compact else 20)
            if self._canvas is None:
                name = "wheel_scroll" if key == "wheel_mode" else key
                button.setIcon(toolbar_icon(name, colors.text, colors.muted, colors.accent))
        self._sync_actions()
