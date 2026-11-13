"""导图桌面运行期间的操作摘要；只保存文字，不持有文档、差量或图片资源。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PureWindowsPath

from PySide6.QtCore import (
    QAbstractListModel,
    QDateTime,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QKeySequence,
    QPainter,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QLabel,
    QListView,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from .palette import UiColors, apply_ui_palette, menu_styles, scrollbar_styles, toolbar_icon

_LEVEL_NAMES = {"info": "信息", "warning": "警告", "error": "错误"}

def _single_line(value, limit=2048):
    return " ".join(str(value).split())[:limit]

@dataclass(frozen=True)
class _LogEntry:
    """不可变的文字记录；界面摘要与完整复制内容分开，均不引用会话或文档。"""

    time: str
    source: str
    level: str
    message: str
    source_path: str = ""
    title: str | None = None
    detail: str = ""

    @property
    def plain_text(self) -> str:
        return f"{self.time} [{_LEVEL_NAMES[self.level]}] [{self.source}] {self.message}"

    @property
    def display_parts(self) -> tuple[str, str]:
        """优先使用应用提供的展示字段；平台不根据业务名称解释日志含义。"""
        if self.title is not None:
            return self.title, self.detail
        title, _, detail = self.message.partition("：")
        return title, detail

    @property
    def source_label(self) -> str:
        return PureWindowsPath(self.source).stem


class _LogModel(QAbstractListModel):
    """有界列表模型，追加及淘汰均发出增量通知，让 Qt 保留未淘汰记录的选择。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.entries: list[_LogEntry] = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.entries)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.entries):
            return None
        entry = self.entries[index.row()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            return entry.plain_text
        if role in (Qt.ItemDataRole.ToolTipRole, Qt.ItemDataRole.AccessibleDescriptionRole):
            return entry.plain_text + ("\n来源：" + entry.source_path if entry.source_path else "")
        if role == Qt.ItemDataRole.UserRole:
            return entry
        return None

    def append(self, entry: _LogEntry, limit: int):
        if len(self.entries) >= limit:
            self.beginRemoveRows(QModelIndex(), 0, 0)
            self.entries.pop(0)
            self.endRemoveRows()
        row = len(self.entries)
        self.beginInsertRows(QModelIndex(), row, row)
        self.entries.append(entry)
        self.endInsertRows()

    def clear(self):
        self.beginResetModel()
        self.entries.clear()
        self.endResetModel()


class _LogDelegate(QStyledItemDelegate):
    """按时间、级别、操作、对象、文件标签绘制记录；窄侧栏自动减少次要列。"""

    def sizeHint(self, option, index):
        return QSize(240, 26 if self.parent()._compact else 30)

    def paint(self, painter: QPainter, option, index):
        panel = self.parent()
        colors, entry = panel._colors, index.data(Qt.ItemDataRole.UserRole)
        rect = option.rect.adjusted(2, 1, -2, -1)
        painter.save()
        try:
            painter.setClipRect(option.rect)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            selected = bool(option.state & QStyle.StateFlag.State_Selected)
            hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
            if selected or hovered or entry.level == "error":
                background = QColor(colors.selected if selected else colors.hover)
                if entry.level == "error" and not (selected or hovered):
                    background = QColor(colors.error)
                    background.setAlpha(12)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(background)
                painter.drawRoundedRect(QRectF(rect), 4, 4)
            left = rect.left() + 8
            if rect.width() >= 360:
                font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
                font.setPixelSize(11)
                painter.setFont(font)
                painter.setPen(QColor(colors.muted))
                painter.drawText(QRect(left, rect.top(), 64, rect.height()), Qt.AlignmentFlag.AlignVCenter, entry.time)
                left += 76
            # 信息使用柔和圆点，异常使用带叉圆圈和警告三角，避免所有行都重复显示“信息”。
            warning = "#d4a34f" if QColor(colors.surface).lightnessF() < 0.5 else "#ae781f"
            color = colors.error if entry.level == "error" else warning if entry.level == "warning" else colors.accent
            center_x, center_y = left + 6, rect.center().y()
            painter.setPen(QPen(QColor(color), 1.3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if entry.level == "warning":
                painter.drawPolygon(QPolygonF([
                    QPointF(center_x, center_y - 6), QPointF(center_x - 6, center_y + 5),
                    QPointF(center_x + 6, center_y + 5),
                ]))
                painter.drawLine(center_x, center_y - 2, center_x, center_y + 1)
                painter.drawPoint(center_x, center_y + 3)
            elif entry.level == "error":
                painter.drawEllipse(QRectF(center_x - 6, center_y - 6, 12, 12))
                painter.drawLine(center_x - 2, center_y - 2, center_x + 2, center_y + 2)
                painter.drawLine(center_x - 2, center_y + 2, center_x + 2, center_y - 2)
            else:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(color))
                painter.drawEllipse(QRectF(center_x - 2.5, center_y - 2.5, 5, 5))
            left += 24
            right = rect.right() - 10
            body_font = QFont(option.font)
            body_font.setPixelSize(12)
            if rect.width() >= 640:
                badge_font = QFont(body_font)
                badge_font.setPixelSize(11)
                painter.setFont(badge_font)
                metrics = QFontMetrics(badge_font)
                width = min(168, metrics.horizontalAdvance(entry.source_label) + 18)
                badge = QRect(right - width, rect.center().y() - 10, width, 20)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(colors.surface if selected else colors.hover))
                painter.drawRoundedRect(QRectF(badge), 4, 4)
                painter.setPen(QColor(colors.muted))
                painter.drawText(badge.adjusted(8, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter,
                                 metrics.elidedText(entry.source_label, Qt.TextElideMode.ElideMiddle, width - 16))
                right = badge.left() - 20
            title, detail = entry.display_parts
            title_font = QFont(body_font)
            title_font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(title_font)
            title_width = min(156, max(48, right - left)) if detail else max(0, right - left)
            title_rect = QRect(left, rect.top(), title_width, rect.height())
            painter.setPen(QColor(color if entry.level != "info" else colors.text))
            painter.drawText(title_rect, Qt.AlignmentFlag.AlignVCenter,
                             QFontMetrics(title_font).elidedText(title, Qt.TextElideMode.ElideRight, title_width))
            if detail:
                detail_rect = QRect(left + title_width + 12, rect.top(),
                                    max(0, right - left - title_width - 12), rect.height())
                painter.setFont(body_font)
                painter.setPen(QColor(colors.muted))
                painter.drawText(detail_rect, Qt.AlignmentFlag.AlignVCenter,
                                 QFontMetrics(body_font).elidedText(detail, Qt.TextElideMode.ElideRight,
                                                                   detail_rect.width()))
            if option.state & QStyle.StateFlag.State_HasFocus:
                painter.setPen(QPen(QColor(colors.accent), 1, Qt.PenStyle.DotLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(QRectF(rect.adjusted(1, 1, -1, -1)), 4, 4)
        finally:
            painter.restore()


class OperationLogPanel(QWidget):
    """全窗口共享的操作列表，所有方法均在 Qt GUI 线程调用。

    本控件只保存当前运行的最近 1000 条文字，既不写磁盘，也不借用会话对象。
    宿主通过 append_entry 提交已经确定来源的摘要；复制和清空只影响日志本身。
    copy_all_action、clear_action 可交给停靠面板作为标题栏动作，生命周期随本控件。
    """

    MAX_ENTRIES = 1000

    def __init__(self, parent: QWidget | None = None):
        """建立空日志；parent 管理 Qt 所有权，实例创建时不恢复任何历史记录。"""
        super().__init__(parent)
        self._compact = False
        self._colors = UiColors()
        self.setObjectName("mindmap_operation_log")
        self.setAccessibleName("操作日志")
        self.list_view = QListView(self)
        self.list_view.setAccessibleName("操作日志记录")
        self.list_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_view.setUniformItemSizes(True)
        self.list_view.setMouseTracking(True)
        self.model = _LogModel(self)
        self.list_view.setModel(self.model)
        self.list_view.setItemDelegate(_LogDelegate(self))
        self.setFocusProxy(self.list_view)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(6, 6, 6, 6)
        self._stack = QStackedWidget(self)
        self.empty_label = QLabel("暂无操作记录\n操作完成后，记录会显示在这里", self)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._stack.addWidget(self.empty_label)
        self._stack.addWidget(self.list_view)
        self._layout.addWidget(self._stack)
        self.copy_action = QAction("复制", self)
        self.copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        self.copy_action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.copy_action.triggered.connect(self.copy_selected)
        self.select_all_action = QAction("全选", self)
        self.select_all_action.setShortcut(QKeySequence.StandardKey.SelectAll)
        self.select_all_action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.select_all_action.triggered.connect(self.list_view.selectAll)
        self.copy_all_action = QAction("复制全部", self)
        self.copy_all_action.setToolTip("复制全部操作日志")
        self.copy_all_action.triggered.connect(self.copy_all)
        self.clear_action = QAction("清空", self)
        self.clear_action.setToolTip("清空本次运行的操作日志")
        self.clear_action.triggered.connect(self.clear)
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        self.list_view.addActions([self.copy_action, self.select_all_action,
                                  self.copy_all_action, self.clear_action])
        self.list_view.selectionModel().selectionChanged.connect(self._sync_actions)
        self._sync_actions()
        self.set_ui_colors(self._colors)

    def append_entry(self, message: str, *, source: str = "应用", level: str = "info",
                     source_path: str = "", title: str | None = None, detail: str = "") -> None:
        """追加一条带本机时间的日志，返回 None，不移动焦点或改变面板显隐。

        message 为操作摘要，最多保留 2048 个字符；source 为记录当时固定的文件名
        或“应用”，不保存会话引用。level 接受 info、warning、error，非法值抛出
        ValueError。source_path 是可选完整路径，仅用于悬停详情，不引用会话对象。
        title/detail 是可选展示字段，应用负责解释业务摘要；复制始终保留完整 message。
        换行转为空格，超过 1000 条时增量删除最旧记录。
        已在底部时跟随新内容；浏览历史时保留原可见行，容量淘汰不造成跳到底部。
        """
        if level not in _LEVEL_NAMES:
            raise ValueError("日志级别必须为 info、warning 或 error")
        following, anchor, offset = self._scroll_anchor()
        stamp = QDateTime.currentDateTime().toString("HH:mm:ss")
        self.model.append(_LogEntry(stamp, _single_line(source, 256), level,
                                    _single_line(message), _single_line(source_path),
                                    _single_line(title, 256) if title is not None else None,
                                    _single_line(detail)), self.MAX_ENTRIES)
        self._sync_actions()
        self._restore_scroll(following, anchor, offset)

    def plain_text(self) -> str:
        """返回当前全部记录的完整纯文本，保留时间、级别、文件名及原始摘要。

        界面省略的路径及长内容仍包含在复制文本中；本方法只读取文字，不改变选择、
        滚动、剪贴板或导图，也不创建文件。空列表返回空字符串。
        """
        return "\n".join(entry.plain_text for entry in self.model.entries)

    def copy_selected(self) -> None:
        """按发生顺序复制所选记录的完整纯文本，支持 Ctrl／Shift 多选及 Ctrl+C。

        选区为空时不修改剪贴板；复制不改变行选择、滚动位置或当前导图。
        """
        indexes = sorted(self.list_view.selectionModel().selectedIndexes(), key=lambda index: index.row())
        if indexes:
            QApplication.clipboard().setText("\n".join(self.model.entries[index.row()].plain_text for index in indexes))

    def clear(self) -> None:
        """清空本次运行的记录并恢复空列表提示，返回 None，不影响导图或剪贴板。"""
        self.model.clear()
        self._sync_actions()

    def copy_all(self) -> None:
        """复制当前保留的全部日志，返回 None；不改变选区、滚动位置或导图内容。

        空日志不覆盖系统剪贴板；非空时仅提交纯文本，不导出文件或保存额外副本。
        """
        if self.model.entries:
            QApplication.clipboard().setText(self.plain_text())

    def set_ui_colors(self, colors: UiColors, *, compact: bool = False) -> None:
        """同步宿主色板和紧凑间距，返回 None，不重建内容、选区或撤销历史。

        colors 必须为宿主提供的界面色板；样式只应用于本面板及其菜单，不修改
        QApplication 或其他组件。紧凑模式的停靠标题尺寸仍由现有框架管理。
        """
        following, anchor, offset = self._scroll_anchor()
        self._colors, self._compact = colors, compact
        apply_ui_palette(self, colors)
        apply_ui_palette(self.list_view, colors)
        self.setStyleSheet(
            f"QListView {{ background:{colors.surface}; border:none; outline:none; }}"
            f"QLabel {{ color:{colors.muted}; font-size:12px; }}"
            + scrollbar_styles(colors) + menu_styles(colors)
        )
        for action, icon in ((self.copy_all_action, "copy"), (self.clear_action, "clear")):
            action.setIcon(toolbar_icon(icon, colors.muted, colors.border, colors.accent))
        self._layout.setContentsMargins(4 if compact else 6, 3 if compact else 5,
                                       4 if compact else 6, 3 if compact else 5)
        self._restore_scroll(following, anchor, offset)
        self.list_view.viewport().update()

    def _scroll_anchor(self):
        """记录顶部记录及像素偏移；模型淘汰更早记录后，持久索引仍指向原来的记录。"""
        scroll = self.list_view.verticalScrollBar()
        index = self.list_view.indexAt(QPoint(0, 0))
        return scroll.value() >= scroll.maximum(), QPersistentModelIndex(index), self.list_view.visualRect(index).top()

    def _restore_scroll(self, following, anchor, offset):
        """同步行高与范围后恢复阅读位置，尾随模式才滚动到底部；不请求焦点。"""
        self.list_view.doItemsLayout()
        if following:
            self.list_view.scrollToBottom()
        elif anchor.isValid():
            self.list_view.scrollTo(QModelIndex(anchor), QAbstractItemView.ScrollHint.PositionAtTop)
            scroll = self.list_view.verticalScrollBar()
            scroll.setValue(scroll.value() - offset)

    def _sync_actions(self):
        """同步空态与动作；标题栏图标及右键操作共用可用状态。"""
        available = bool(self.model.entries)
        for action in (self.select_all_action, self.copy_all_action, self.clear_action):
            action.setEnabled(available)
        self.copy_action.setEnabled(bool(self.list_view.selectionModel().selectedIndexes()))
        self._stack.setCurrentIndex(1 if available else 0)
