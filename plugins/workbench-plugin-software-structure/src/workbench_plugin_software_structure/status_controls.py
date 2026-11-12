"""工作台状态栏右侧的模块依赖图缩放控件。"""

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QHBoxLayout, QToolButton, QWidget
from workbench_api import EditorView
from workbench_api.qt import UiService

from .module_deps.canvas import ModuleDependencyCanvas
from .viewer import SoftwareStructureView


class SoftwareStructureStatusControls(QWidget):
    """每个工作台窗口复用一组按钮，缩放状态由当前软件结构画布提供。"""

    def __init__(self, ui: UiService):
        """创建可用 Tab 键访问的按钮，鼠标点击时保留画布焦点。"""
        super().__init__()
        self._ui = ui
        self._canvas: ModuleDependencyCanvas | None = None
        self.setObjectName("software_structure_status_controls")
        self.setAccessibleName("模块依赖图视图操作")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(2)

        self._fit_button = QToolButton(self)
        self._fit_button.setObjectName("status_fit")
        self._fit_button.setIconSize(QSize(16, 16))
        self._fit_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._fit_button.setToolTip("适应窗口")
        self._fit_button.setAccessibleName("适应窗口")
        self._fit_button.clicked.connect(self._fit_to_view)

        self._zoom_button = QToolButton(self)
        self._zoom_button.setObjectName("status_reset_zoom")
        self._zoom_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._zoom_button.setAccessibleName("恢复默认尺寸")
        self._zoom_button.clicked.connect(self._reset_zoom)

        for button in (self._fit_button, self._zoom_button):
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.setEnabled(False)
            layout.addWidget(button)
        self._sync_zoom()

    def bind_view(self, view: EditorView | None) -> None:
        """切换活动画布。空图禁用两个按钮，关闭标签时释放对旧画布的引用。"""
        canvas = None
        if isinstance(view, SoftwareStructureView) and view.document.content.modules:
            canvas = view.widget
        if canvas is self._canvas:
            return
        # 同时可见的分屏各有视口。先断开旧信号，再让新画布驱动按钮读数。
        if self._canvas is not None:
            self._canvas.viewport_changed.disconnect(self._sync_zoom)
        self._canvas = canvas
        if canvas is not None:
            canvas.viewport_changed.connect(self._sync_zoom)
        self._fit_button.setEnabled(canvas is not None)
        self._zoom_button.setEnabled(canvas is not None)
        self._sync_zoom()

    def apply_theme(self, *_) -> None:
        """使用宿主图标和色板，并按普通或紧凑状态栏设置逻辑像素尺寸。"""
        hover = self._ui.theme_color("toolbar-bg-hovered").name()
        pressed = self._ui.theme_color("toolbar-selected-bg-active").name()
        focus = self._ui.theme_color("control-brand-border").name()
        self.setStyleSheet(
            "QToolButton { background:transparent; border:1px solid transparent; border-radius:4px; padding:0; }"
            f"QToolButton:hover {{ background:{hover}; }}"
            f"QToolButton:pressed {{ background:{pressed}; }}"
            f"QToolButton:focus {{ border-color:{focus}; }}"
        )
        # 与思维导图状态栏读取同一个主题资源，兼容尚未提供该图标的宿主。
        fit_icon = self._ui.theme_icon("fit-view")
        self._fit_button.setIcon(self._ui.theme_icon("maximize") if fit_icon.isNull() else fit_icon)
        compact = self._ui.compact
        height = 18 if compact else 20
        self._fit_button.setFixedSize(22 if compact else 24, height)
        # 预留最大交互倍率的文字宽度，缩放时按钮位置保持稳定。
        self._zoom_button.setFixedSize(self._zoom_button.fontMetrics().horizontalAdvance("400%") + 10, height)

    def _sync_zoom(self) -> None:
        """读取画布实际变换。平移也会发出信号，显示文本相同时跳过界面更新。"""
        if self._canvas is None:
            text, description = "—", "当前文档没有模块"
        else:
            zoom = self._canvas.zoom_factor
            text = f"{zoom:.0%}" if zoom >= 0.01 else "<1%"
            description = f"当前缩放 {text}，点击恢复默认尺寸（100%）"
        if text != self._zoom_button.text():
            self._zoom_button.setText(text)
            self._zoom_button.setToolTip(description)
            self._zoom_button.setAccessibleDescription(description)

    def _fit_to_view(self) -> None:
        if self._canvas is not None:
            self._canvas.fit_to_view()

    def _reset_zoom(self) -> None:
        if self._canvas is not None:
            self._canvas.reset_zoom()
