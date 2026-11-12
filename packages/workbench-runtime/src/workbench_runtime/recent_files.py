"""窗控旁的最近文件入口，复用工作台打开流程与外壳主题。"""

from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QSize, Qt
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QMenu, QSizePolicy, QToolButton, QWidget, QWidgetAction
from workbench_api import path_key


class RecentFilesButton(QToolButton):
    """显示当前文件并展开历史；只负责呈现，不直接加载文件或写入会话配置。"""

    # 文字之外的宽度包括边框、左右内边距、文档图标、图文间距和菜单箭头。
    _TEXT_INSET = 56
    _MIN_WIDTH = 80
    _MAX_WIDTH = 280
    _TEMPORARY_FOCUS_REASONS = (Qt.FocusReason.PopupFocusReason, Qt.FocusReason.ActiveWindowFocusReason)

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self._full_text = "最近文件"
        self.setObjectName("mindmap_recent_files")
        self.setProperty("recentMenuOpen", False)
        self.setProperty("recentKeyboardFocus", False)
        self.setFixedHeight(32)
        self.setMinimumWidth(self._MIN_WIDTH)
        self.setMaximumWidth(self._MAX_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setIconSize(QSize(16, 16))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        menu = _RecentFilesMenu(self)
        menu.setToolTipsVisible(True)
        menu.setAccessibleName("最近文件")
        menu.aboutToShow.connect(self._populate_menu)
        # 菜单展开后接管鼠标，独立的展开状态让按钮继续显示浅底，关闭时恢复。
        menu.aboutToShow.connect(lambda: self._set_style_flag("recentMenuOpen", True))
        menu.aboutToHide.connect(lambda: self._set_style_flag("recentMenuOpen", False))
        self.setMenu(menu)
        self.refresh_theme()
        self.refresh_document()

    def sizeHint(self):
        """按完整文件名估算宽度，避免省略后的短文字反过来使布局不断收缩。"""
        width = self.fontMetrics().horizontalAdvance(self._full_text) + self._TEXT_INSET
        return QSize(min(self._MAX_WIDTH, max(self._MIN_WIDTH, width)), 32)

    def _update_text(self):
        """只省略可见文字，悬停提示和辅助技术仍使用完整文件名或路径。"""
        # 窗口尚未显示时，按钮可能仍保留构造尺寸。使用布局期望宽度，避免占位文字过早省略。
        width = self.width() if self.isVisible() else self.sizeHint().width()
        label = self.fontMetrics().elidedText(self._full_text, Qt.TextElideMode.ElideMiddle,
                                             width - self._TEXT_INSET)
        self.setText(label.replace("&", "&&"))

    def _set_style_flag(self, name: str, enabled: bool) -> None:
        """仅在交互属性改变时重新应用本地样式，保留 Qt 原有的焦点与菜单行为。"""
        if self.property(name) == enabled:
            return
        self.setProperty(name, enabled)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def refresh_document(self):
        """活动标签、关闭及另存为后更新显示；历史重排只由成功的文件操作触发。"""
        session = self.window.documents.active
        path = session.document.path if session else None
        text = self.window.documents.display_title(session, file_name=True) if path else "最近文件"
        tooltip = str(path) if path else "查看最近打开的文件"
        # 引擎修改和选区变化也会刷新窗口状态；路径没有变化时不反复触发布局计算。
        if self._full_text == text and self.toolTip() == tooltip:
            return
        self._full_text = text
        self.setToolTip(tooltip)
        self.setAccessibleName(f"{self._full_text}，展开最近文件")
        self.setIcon(self.window.file_icon(path))
        self._update_text()
        self.updateGeometry()

    def refresh_theme(self):
        """菜单独立同步主题，使弹出窗口在明暗切换和紧凑模式下继续使用正确色板。"""
        theme = self.window.manager.theme
        active = self.window.documents.active
        self.setIcon(self.window.file_icon(active.document.path if active else None))
        # 默认与标题栏融为一体，交互时使用窗控的中性底色。透明边框为键盘焦点留出
        # 一像素位置，焦点切换时图标、文件名和箭头都保持原位。
        self.setStyleSheet(f"""
            QToolButton#mindmap_recent_files {{
                background:transparent;
                border:1px solid transparent;
                border-radius:6px;
                padding:0 24px 0 8px;
            }}
            QToolButton#mindmap_recent_files:hover,
            QToolButton#mindmap_recent_files:pressed,
            QToolButton#mindmap_recent_files[recentMenuOpen="true"] {{
                background:{theme.css('window-control-bg-hovered')};
            }}
            QToolButton#mindmap_recent_files[recentKeyboardFocus="true"]:focus {{
                border-color:{theme.css('control-brand-border')};
            }}
            QToolButton#mindmap_recent_files::menu-indicator {{
                subcontrol-position:right center; right:7px;
            }}
        """)
        theme.apply(self.menu(), "QMenu { menu-scrollable:1; }")
        self._update_text()

    def focusInEvent(self, event):
        """为键盘导航显示焦点描边。关闭弹层或重新激活窗口时延续原来的焦点来源。"""
        reason = event.reason()
        if reason not in self._TEMPORARY_FOCUS_REASONS:
            keyboard = reason in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                                  Qt.FocusReason.ShortcutFocusReason)
            self._set_style_flag("recentKeyboardFocus", keyboard)
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        """主动离开按钮时清理焦点来源，避免日后自动聚焦时恢复旧描边。"""
        if event.reason() not in self._TEMPORARY_FOCUS_REASONS:
            self._set_style_flag("recentKeyboardFocus", False)
        super().focusOutEvent(event)

    def mousePressEvent(self, event):
        """按钮已获键盘焦点时，鼠标按下也能立即切回指针反馈。"""
        self._set_style_flag("recentKeyboardFocus", False)
        super().mousePressEvent(event)

    def _populate_menu(self):
        """每次展开按最新历史生成双行文件项，完整路径继续用于打开文件和悬停提示。"""
        menu = self.menu()
        menu.clear()
        active = self.window.documents.active
        current_key = path_key(active.document.path) if active and active.document.path else None
        # 给弹层边框和内边距留出空间。行宽随内容增长，长路径在上限内单独省略。
        width_limit = max(1, min(520, self.screen().availableGeometry().width() - 24))
        recent = self.window.state.recent_files
        for value in recent:
            path = Path(value)
            action = QWidgetAction(menu)
            # 动作保留完整文本供辅助技术读取。可见行独立绘制，两处都将 & 作为普通字符。
            action.setText(f"{path.name}\n{path.parent}".replace("&", "&&"))
            action.setData(value)
            action.setToolTip(value)
            action.setCheckable(True)
            action.setChecked(path_key(path) == current_key)
            action.setDefaultWidget(_RecentFileRow(menu, action, path, width_limit))
            action.triggered.connect(lambda checked=False, selected=value:
                                     self.window.call(lambda: self.window.open_path(selected)))
            menu.addAction(action)
        if not recent:
            menu.addAction("暂无最近文件").setEnabled(False)
        menu.addSeparator()
        menu.addAction("打开文件…", lambda: self.window.call(self.window.open_dialog))
        clear = menu.addAction("清空最近记录", self.window.clear_recent_files)
        clear.setEnabled(bool(recent))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_text()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and hasattr(self, "_full_text"):
            self._update_text()
            self.updateGeometry()


class _RecentFilesMenu(QMenu):
    """使用 Qt 的菜单交互，按文档按钮的右边缘对齐弹层。"""

    @property
    def workbench(self):
        """通过所属按钮取得宿主，菜单绘制始终读取窗口当前的主题及文件提供者。"""
        return self.parentWidget().window

    @property
    def theme(self):
        return self.workbench.manager.theme

    def showEvent(self, event):
        """Qt 决定垂直方向和可见高度，水平方向按按钮所在屏幕的工作区约束。"""
        super().showEvent(event)
        button = self.parentWidget()
        bounds = button.screen().availableGeometry()
        anchor_right = button.mapToGlobal(QPoint(button.width(), 0)).x()
        left = max(bounds.left(), min(anchor_right, bounds.right() + 1) - self.width())
        self.move(left, self.y())


class _RecentFileRow(QWidget):
    """绘制文件名、目录和当前文件标记，点击与键盘选择交由所属菜单。"""

    _TEXT_LEFT = 34
    _TEXT_RIGHT = 32

    def __init__(self, menu: _RecentFilesMenu, action: QWidgetAction, path: Path, width_limit: int):
        super().__init__()
        self._menu = menu
        self._action = action
        self._path = path
        self._file_name = path.name
        self._directory = str(path.parent)
        self._width_limit = width_limit
        # 菜单通过 QAction 统一处理选中、方向键、回车、鼠标命中及悬停提示。
        # 行控件只负责绘制，不抢占菜单的焦点，也不吞掉指针事件。
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName(f"{self._file_name}，{self._directory}")

    def _directory_font(self) -> QFont:
        """目录沿用主字体，略缩字号。兼顾像素和点数两种主题字体配置。"""
        font = self.font()
        if font.pixelSize() > 0:
            font.setPixelSize(max(1, font.pixelSize() - 1))
        else:
            font.setPointSizeF(max(1.0, font.pointSizeF() - 1))
        return font

    def sizeHint(self) -> QSize:
        """两行分别按字体度量计算尺寸，屏幕缩放和紧凑模式下也保留完整行高。"""
        name_metrics = self.fontMetrics()
        directory_metrics = QFontMetrics(self._directory_font(), self)
        text_width = max(name_metrics.horizontalAdvance(self._file_name),
                         directory_metrics.horizontalAdvance(self._directory))
        width = max(288, text_width + self._TEXT_LEFT + self._TEXT_RIGHT)
        padding = 8 if self._menu.theme.compact else 12
        return QSize(min(self._width_limit, width), name_metrics.height() + directory_metrics.height() + 1 + padding)

    def paintEvent(self, event):
        """当前文件的勾选和底色保持可见，鼠标或键盘定位使用独立反馈。"""
        theme = self._menu.theme
        checked = self._action.isChecked()
        active = self._menu.activeAction() is self._action
        rect = self.rect()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if checked or active:
            painter.setPen(theme.color("control-brand-border") if checked and active else Qt.PenStyle.NoPen)
            painter.setBrush(theme.color("toolbar-selected-bg-active" if checked else "toolbar-bg-hovered"))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 5, 5)
        # 菜单行绘制时再解析图标，已关闭的文件仍按类型显示，并使用当前主题颜色。
        self._menu.workbench.file_icon(self._path).paint(painter, QRect(10, (rect.height() - 16) // 2, 16, 16))

        # 分别省略文件名和目录，避免长文件名把目录或右侧勾选挤出菜单。
        name_metrics = self.fontMetrics()
        directory_font = self._directory_font()
        directory_metrics = QFontMetrics(directory_font, self)
        width = max(0, rect.width() - self._TEXT_LEFT - self._TEXT_RIGHT)
        top = (rect.height() - name_metrics.height() - directory_metrics.height() - 1) // 2
        name_rect = QRect(self._TEXT_LEFT, top, width, name_metrics.height())
        directory_rect = QRect(self._TEXT_LEFT, name_rect.bottom() + 2, width, directory_metrics.height())
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        painter.setPen(theme.color("text-default"))
        painter.drawText(name_rect, align, name_metrics.elidedText(self._file_name, Qt.TextElideMode.ElideMiddle, width))
        painter.setFont(directory_font)
        painter.setPen(theme.color("text-muted"))
        painter.drawText(directory_rect, align,
                         directory_metrics.elidedText(self._directory, Qt.TextElideMode.ElideMiddle, width))

        if checked:
            # 使用矢量线段绘制勾选，避免主题字体缺少符号或在高 DPI 下缩放模糊。
            x, y = rect.right() - 16, rect.center().y()
            painter.setPen(QPen(theme.color("control-brand-border"), 1.6, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPolyline(QPolygonF([QPointF(x - 5, y), QPointF(x - 1, y + 4), QPointF(x + 5, y - 3)]))
