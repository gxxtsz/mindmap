"""属性表单的紧凑控件；只管理显示和用户输入，不访问文档或历史。"""

from __future__ import annotations

from mindmap_engine import BranchStyle, Theme
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontDatabase, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QCompleter,
    QLayout,
    QMenu,
    QSizePolicy,
    QToolButton,
    QWidgetItem,
)

from .branch_styles import BRANCH_POPUP_PADDING, BranchStyleMenu
from .ui_style import ComboBox, ui_colors

MIXED = object()


class FlowLayout(QLayout):
    """按可用逻辑宽度自动换行，避免窄侧栏被字体行的最小宽度撑开。

    每行的剩余空间只分配给可扩展控件；高度同时考虑字段下方的错误提示。
    通过 heightForWidth 告知滚动区实际高度，因此不需要横向滚动条或固定面板宽度。
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(6)

    def addItem(self, item):
        self.items.append(item)

    def addWidget(self, widget):
        self.addChildWidget(widget)
        self.addItem(QWidgetItem(widget))

    def count(self):
        return len(self.items)

    def itemAt(self, index):
        return self.items[index] if 0 <= index < len(self.items) else None

    def takeAt(self, index):
        return self.items.pop(index) if 0 <= index < len(self.items) else None

    def expandingDirections(self):
        return Qt.Orientation.Horizontal

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), measure=True)

    def minimumSize(self):
        size = QSize()
        for item in self.items:
            size = size.expandedTo(item.minimumSize())
        return size

    def sizeHint(self):
        return QSize(300, self.heightForWidth(300))

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect)

    def _arrange(self, rect, *, measure=False):
        available, spacing = max(1, rect.width()), self.spacing()
        lines, line, used = [], [], 0
        for item in self.items:
            if item.isEmpty():
                continue
            width = min(available, item.sizeHint().width())
            if line and used + spacing + width > available:
                lines.append(line)
                line, used = [], 0
            used += width + (spacing if line else 0)
            line.append((item, width))
        if line:
            lines.append(line)
        y = rect.y()
        for line in lines:
            growing = sum(bool(item.expandingDirections() & Qt.Orientation.Horizontal) for item, _ in line)
            extra = max(0, available - sum(width for _, width in line) - spacing * (len(line) - 1))
            widths = [
                width
                + (
                    extra // growing
                    if growing and item.expandingDirections() & Qt.Orientation.Horizontal
                    else 0
                )
                for item, width in line
            ]
            height = max(
                item.heightForWidth(width) if item.hasHeightForWidth() else item.sizeHint().height()
                for (item, _), width in zip(line, widths, strict=True)
            )
            x = rect.x()
            for (item, _), width in zip(line, widths, strict=True):
                if not measure:
                    item.setGeometry(QRect(x, y, width, height))
                x += width + spacing
            y += height + spacing
        return max(0, y - rect.y() - (spacing if lines else 0))


class FontPicker(ComboBox):
    """支持搜索、任意字体名称及未配置状态，不能把“默认字体”文字写进样式。

    changed 的布尔参数区分明确选项与连续输入；后者交给侧栏进行防抖及输入法保护。
    程序调用 set_value 不发送 changed，未安装的字体名称也能无损显示和保存。
    """

    changed = Signal(object, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(8)
        self.setMinimumWidth(110)
        self.setMinimumHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.addItem("默认字体", "")
        for family in QFontDatabase.families():
            self.addItem(family, family)
        self.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._value = ""
        self.lineEdit().textEdited.connect(self._typed)
        self.activated.connect(self._activated)

    def sizeHint(self):
        return QSize(140, super().sizeHint().height())

    def _typed(self, text):
        self._value = text
        self.changed.emit(text, False)

    def _activated(self, index):
        # 可编辑组合框在按回车时可能仍保留旧索引；任意输入不能被旧选项覆盖。
        self._value = (
            self.itemData(index)
            if index >= 0 and self.currentText() == self.itemText(index)
            else self.currentText()
        )
        self.changed.emit(self._value, True)

    def set_value(self, value):
        self._value = value
        self.setCurrentIndex(-1 if value is MIXED else self.findData(value))
        self.setEditText("" if value is MIXED else "默认字体" if value == "" else value)
        self.lineEdit().setPlaceholderText("多种值" if value is MIXED else "默认字体")

    def value(self):
        return self._value


class ToggleButton(QToolButton):
    """布尔图标按钮的混合状态只用于展示；从混合状态点击后统一设置为启用。"""

    changed = Signal(bool)

    def __init__(self, label, parent=None):
        super().__init__(parent)
        self.label = label
        self.setCheckable(True)
        self.setFixedSize(34, 34)
        self.setIconSize(QSize(22, 22))
        self.setAccessibleName(label)
        self.clicked.connect(self._clicked)

    def _clicked(self, checked):
        self.set_value(checked)
        self.changed.emit(checked)

    def set_value(self, value):
        self.setChecked(value is not MIXED and bool(value))
        self.setProperty("mixed", value is MIXED)
        self.setToolTip(self.label + ("：多种值" if value is MIXED else ""))
        self.style().unpolish(self)
        self.style().polish(self)

    def value(self):
        return MIXED if self.property("mixed") else self.isChecked()


class ColorButton(QToolButton):
    """用色块表达显式颜色、主题继承或混合值，取消颜色对话框不产生修改。

    对话框打开前绑定显示代次；期间若选择、文档或只读状态变化，放弃旧结果，
    防止嵌套事件循环结束后把原节点的颜色写给当前新选择。
    """

    changed = Signal(object)

    def __init__(self, label, *, text_color=False, parent=None):
        super().__init__(parent)
        self.label, self.text_color = label, text_color
        self._value, self._version = "", 0
        self._theme = None
        self.setProperty("color_swatch", not text_color)
        self.setMinimumSize(54 if text_color else 80, 34)
        self.setMaximumWidth(62 if text_color else 96)
        self.setIconSize(QSize(28 if text_color else 60, 22))
        self.setAccessibleName(label)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self)
        self.choose_action = menu.addAction("选择颜色…", self.choose_color)
        self.reset_action = menu.addAction("恢复主题颜色", lambda: self.select_color(""))
        self.setMenu(menu)

    def choose_color(self):
        version = self._version
        initial = (
            QColor(self._value) if self._value is not MIXED and self._value else QColor(self._theme.accent)
        )
        color = QColorDialog.getColor(
            initial, self, self.label, QColorDialog.ColorDialogOption.ShowAlphaChannel
        )
        if version == self._version and self.isEnabled() and color.isValid():
            self.select_color(
                color.name(QColor.NameFormat.HexArgb if color.alpha() < 255 else QColor.NameFormat.HexRgb)
            )

    def select_color(self, value):
        """明确选择或恢复主题色时才发出信号；值的最终验证仍由提交控制器负责。"""
        if self.isEnabled():
            self.set_value(value)
            self.changed.emit(value)

    def set_value(self, value):
        self._value = value
        self._version += 1
        self._update_icon()

    def value(self):
        return self._value

    def set_theme(self, theme):
        self._theme = theme
        self._update_icon()

    def _update_icon(self):
        if self._theme is None:
            return
        theme, value = self._theme, self._value
        colors = ui_colors(theme)
        title = "多种值" if value is MIXED else "继承主题" if not value else value
        self.setToolTip(f"{self.label}：{title}")
        icon = QIcon()
        for ratio in (1.0, 1.5, 2.0, 3.0):
            size = self.iconSize()
            pixmap = QPixmap(round(size.width() * ratio), round(size.height() * ratio))
            pixmap.setDevicePixelRatio(ratio)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(1, 2, size.width() - 2, size.height() - 4)
            color = QColor(value) if value is not MIXED and value else QColor(colors.field)
            if not color.isValid():
                color = QColor(colors.field)
            if self.text_color:
                painter.setPen(QColor(colors.text))
                font = painter.font()
                font.setBold(True)
                font.setPixelSize(20)
                painter.setFont(font)
                painter.drawText(rect.adjusted(0, -2, 0, -3), Qt.AlignmentFlag.AlignCenter, "A")
                rect = QRectF(3, size.height() - 5, size.width() - 6, 4)
            painter.setPen(QPen(QColor(colors.border), 1))
            painter.setBrush(color)
            painter.drawRoundedRect(rect, 3 if not self.text_color else 1, 3 if not self.text_color else 1)
            if value is MIXED or not value:
                painter.save()
                painter.setClipRect(rect)
                painter.setPen(QPen(QColor(colors.text if value is MIXED else colors.border), 1))
                for x in range(-size.height(), size.width(), 6):
                    painter.drawLine(x, size.height(), x + size.height(), 0)
                painter.restore()
            painter.end()
            icon.addPixmap(pixmap)
        self.setIcon(icon)


class BranchPicker(QComboBox):
    """收起显示当前图标，展开四列十一项网格，复用原有值模型和激活信号。

    底层选项继续供共享表单及弹窗读取；网格确认复用 activated，悬停不触发提交。
    set_value 用于快照刷新，会先取消弹层，避免旧选择、只读状态或文档替换后
    残留的菜单操作写入新目标。悬停仅提供反馈，不提交或修改当前值。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setIconSize(QSize(40, 40))
        self.setMinimumSize(94, 46)
        self.setMaximumWidth(104)
        self._theme = Theme()
        self.popup_menu = BranchStyleMenu(self, compact=True)
        self.popup_menu.style_selected.connect(self._choose_kind)
        self.popup_menu.aboutToHide.connect(self._popup_closed)
        self.currentIndexChanged.connect(lambda: self.setToolTip(self.currentText()))

    def set_theme(self, theme):
        self.hidePopup()
        self._theme = theme

    def set_value(self, value):
        """同步单值或混合值，仅刷新显示，不发出用户选择事件。"""
        self.hidePopup()
        index = self.findData("__mixed__")
        if index >= 0:
            self.removeItem(index)
        if value is MIXED:
            self.insertItem(0, "多种样式", "__mixed__")
            self.model().item(0).setEnabled(False)
            self.setCurrentIndex(0)
        else:
            self.setCurrentIndex(max(0, self.findData(value)))
        self.setToolTip(self.currentText())

    def value(self):
        return MIXED if self.currentData() == "__mixed__" else self.currentData()

    def showPopup(self):
        """右边缘对齐入口；Qt 菜单负责屏幕边缘避让、外部点击及 Escape 取消。"""
        if not self.isEnabled():
            return
        value = self.value()
        current = BranchStyle(value) if value is not MIXED and value is not None else None
        menu = self.popup_menu
        menu.sync(set() if value is MIXED else {current}, "subtree", self._theme)
        menu.ensurePolished()
        menu.adjustSize()
        # 以可见面板的右边缘对齐入口，透明阴影留白不计入对齐边界。
        menu.popup(
            self.mapToGlobal(QPoint(self.width() - menu.width() + BRANCH_POPUP_PADDING, self.height() - 4))
        )
        target = (
            menu.buttons[current]
            if current is not None
            else menu.reset
            if value is None
            else next(iter(menu.buttons.values()))
        )
        target.setFocus(Qt.FocusReason.PopupFocusReason)

    def hidePopup(self):
        menu = getattr(self, "popup_menu", None)
        if menu is not None and menu.isVisible():
            menu.close()
        super().hidePopup()

    def _popup_closed(self):
        # 自定义弹层关闭后恢复组合框内部状态，下一次鼠标或键盘仍能正常打开。
        super().hidePopup()
        self.update()

    def _choose_kind(self, kind):
        if not self.isEnabled() or not self.popup_menu.isVisible():
            return
        index = self.findData(kind)
        if index >= 0:
            self.hidePopup()
            self.setCurrentIndex(index)
            self.activated.emit(index)

    def keyPressEvent(self, event):
        if event.key() in (
            Qt.Key.Key_Space,
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Down,
            Qt.Key.Key_Up,
        ):
            self.showPopup()
            event.accept()
        else:
            super().keyPressEvent(event)

    def hideEvent(self, event):
        self.hidePopup()
        super().hideEvent(event)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.EnabledChange and not self.isEnabled():
            self.hidePopup()
        super().changeEvent(event)

    def sizeHint(self):
        return QSize(98, 46)

    def minimumSizeHint(self):
        return QSize(94, 46)

    def paintEvent(self, event):
        """入口统一绘制圆角边框与小箭头，避免原生组合框的分隔线破坏图标按钮外观。"""
        colors = ui_colors(self._theme)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(colors.accent if self.hasFocus() else colors.border), 1))
        painter.setBrush(QColor(colors.field))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
        foreground = QColor(colors.text if self.isEnabled() else colors.muted)
        if self.currentData() == "__mixed__":
            painter.setPen(foreground)
            # 混合值沿用模型中的完整中文名称，整图与局部入口使用一致的可见提示。
            painter.drawText(
                self.rect().adjusted(4, 0, -25, 0), Qt.AlignmentFlag.AlignCenter, self.currentText()
            )
        else:
            icon = self.itemIcon(self.currentIndex())
            icon.paint(
                painter,
                QRect(10, (self.height() - 40) // 2, 40, 40),
                Qt.AlignmentFlag.AlignCenter,
                QIcon.Mode.Normal if self.isEnabled() else QIcon.Mode.Disabled,
                QIcon.State.Off,
            )
        x, y = self.width() - 15, self.height() / 2
        painter.setPen(
            QPen(foreground, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        )
        painter.drawPolyline(
            QPolygonF([QPointF(x - 3.5, y - 1.5), QPointF(x, y + 2), QPointF(x + 3.5, y - 1.5)])
        )
