"""紧邻主菜单的最近导图入口，复用工作台打开流程与外壳主题。"""

from pathlib import Path

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtWidgets import QMenu, QSizePolicy, QToolButton

from .document_state import path_key


class RecentFilesButton(QToolButton):
    """显示当前文件并展开历史；只负责呈现，不直接加载文件或写入会话配置。"""

    # 只为左侧文字留白、右侧箭头和边框预留空间，不再计算文档图标的占位。
    _TEXT_INSET = 36

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self._full_text = "最近文件"
        self.setObjectName("mindmap_recent_files")
        self.setProperty("titleBarAction", True)
        self.setProperty("recentMenuOpen", False)
        self.setFixedHeight(32)
        self.setMinimumWidth(80)
        self.setMaximumWidth(280)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        menu.aboutToShow.connect(self._populate_menu)
        menu.aboutToShow.connect(lambda: self._set_menu_open(True))
        menu.aboutToHide.connect(lambda: self._set_menu_open(False))
        self.setMenu(menu)
        self.refresh_theme()
        self.refresh_document()

    def sizeHint(self):
        """按完整文件名估算宽度，避免省略后的短文字反过来使布局不断收缩。"""
        width = self.fontMetrics().horizontalAdvance(self._full_text) + self._TEXT_INSET
        return QSize(min(280, max(80, width)), 32)

    def _update_text(self):
        self.setText(self.fontMetrics().elidedText(self._full_text, Qt.TextElideMode.ElideMiddle,
                                                 max(24, self.width() - self._TEXT_INSET)))

    def _set_menu_open(self, opened: bool):
        """菜单展开期间保持灰色底，即使鼠标已经移到菜单内也不撤销按钮反馈。

        使用独立外观属性而非可勾选状态，不改变 QToolButton 原有的鼠标及键盘
        触发行为；隐藏菜单后重新计算样式，由实际悬停状态决定是否继续显示灰底。
        """
        if self.property("recentMenuOpen") == opened:
            return
        self.setProperty("recentMenuOpen", opened)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def refresh_document(self):
        """活动标签、关闭及另存为后更新显示；历史重排只由成功的文件操作触发。"""
        session = self.window.active_session
        path = session.files.path if session else None
        text = path.stem if path else "最近文件"
        tooltip = str(path) if path else "查看最近打开的思维导图"
        # 引擎修改和选区变化也会刷新窗口状态；路径没有变化时不反复触发布局计算。
        if self._full_text == text and self.toolTip() == tooltip:
            return
        self._full_text = text
        self.setToolTip(tooltip)
        self.setAccessibleName(f"{self._full_text}，展开最近文件")
        self._update_text()
        self.updateGeometry()

    def refresh_theme(self):
        """菜单独立同步主题，使弹出窗口在明暗切换和紧凑模式下继续使用正确色板。"""
        theme = self.window.manager.theme
        # 文件切换器只绘制名称和箭头。悬停、按下及展开统一使用灰色反馈，覆盖
        # 顶栏通用按钮的蓝色按下背景；颜色沿用窗口控制的主题令牌以适配深浅外观。
        self.setStyleSheet(f"""
            QToolButton#mindmap_recent_files {{ padding:0 24px 0 8px; }}
            QToolButton#mindmap_recent_files:hover,
            QToolButton#mindmap_recent_files:pressed,
            QToolButton#mindmap_recent_files[recentMenuOpen="true"] {{
                background:{theme.css('window-control-bg-hovered')};
            }}
            QToolButton#mindmap_recent_files::menu-indicator {{
                subcontrol-position:right center; right:7px;
            }}
        """)
        theme.apply(self.menu(), "QMenu { menu-scrollable:1; }")
        self._update_text()

    def _populate_menu(self):
        """每次展开读取最新历史；完整路径放入数据和提示，显示文本限制在屏幕内。"""
        menu = self.menu()
        menu.clear()
        active = self.window.files.path if self.window.files else None
        current_key = path_key(active) if active else None
        screen_width = self.screen().availableGeometry().width()
        text_width = max(180, min(640, screen_width - 24) - 80)
        for value in self.window.document_state.recent_files:
            path = Path(value)
            label = f"{path.name}  —  {path.parent}"
            label = menu.fontMetrics().elidedText(label, Qt.TextElideMode.ElideMiddle, text_width)
            # 文件名中的 & 是普通字符，不能被 QMenu 误当成键盘助记符。
            action = menu.addAction(label.replace("&", "&&"))
            action.setData(value)
            action.setToolTip(value)
            action.setCheckable(True)
            action.setChecked(path_key(path) == current_key)
            action.triggered.connect(lambda checked=False, selected=value:
                                     self.window.call(lambda: self.window.open_path(selected)))
        if not self.window.document_state.recent_files:
            menu.addAction("暂无最近文件").setEnabled(False)
        menu.addSeparator()
        menu.addAction("打开文件…", lambda: self.window.call(self.window.open_dialog))
        clear = menu.addAction("清空最近记录", self.window.clear_recent_files)
        clear.setEnabled(bool(self.window.document_state.recent_files))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_text()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and hasattr(self, "_full_text"):
            self._update_text()
            self.updateGeometry()
