"""导图布局的图形选择器与间距输入；控件只发送用户意图，不持有文档。"""

from mindmap_core import Theme
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QActionGroup,
    QColor,
    QIcon,
    QInputMethodEvent,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import QApplication, QDoubleSpinBox, QStyle, QStyleOptionSpinBox

from .settings_menu import SettingsMenu
from .ui_style import ComboBox, ui_colors


def _layout_icon(direction, colors):
    """以统一矢量画布绘制结构图，为紧凑入口及菜单提供清晰的小图标。

    普通、禁用状态均提供多档设备像素比；中心主题使用强调色，其余主题和连线
    使用中性色。绘制只依赖方向与色板，可同时用于入口预览及菜单小图示。
    """
    icon = QIcon()
    for disabled in (False, True):
        for ratio in (1, 1.5, 2, 3):
            pixmap = QPixmap(round(72 * ratio), round(48 * ratio))
            pixmap.setDevicePixelRatio(ratio)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            if direction == "left":
                painter.translate(72, 0)
                painter.scale(-1, 1)
            if direction == "both":
                root = QRectF(29, 18, 14, 12)
                children = [QRectF(x, y, 13, 9) for x in (4, 55) for y in (4, 35)]
                paths = [((29, 24), (22, 24), (22, y + 4.5), (17, y + 4.5)) for y in (4, 35)]
                paths += [((43, 24), (50, 24), (50, y + 4.5), (55, y + 4.5)) for y in (4, 35)]
            elif direction == "down":
                root = QRectF(28, 3, 16, 12)
                children = [QRectF(x, 34, 16, 10) for x in (8, 48)]
                paths = [((36, 15), (36, 24), (x + 8, 24), (x + 8, 34)) for x in (8, 48)]
            else:
                root = QRectF(4, 18, 16, 12)
                children = [QRectF(49, y, 18, 10) for y in (4, 34)]
                paths = [((20, 24), (34, 24), (34, y + 5), (49, y + 5)) for y in (4, 34)]
            painter.setPen(QPen(QColor(colors.muted), 1.5, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            for points in paths:
                path = QPainterPath(QPointF(*points[0]))
                for point in points[1:]:
                    path.lineTo(QPointF(*point))
                painter.drawPath(path)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colors.border if disabled else colors.muted))
            for rect in children:
                painter.drawRoundedRect(rect, 2.5, 2.5)
            painter.setBrush(QColor(colors.muted if disabled else colors.accent))
            painter.drawRoundedRect(root, 3, 3)
            painter.end()
            icon.addPixmap(pixmap, QIcon.Mode.Disabled if disabled else QIcon.Mode.Normal)
    return icon


class LayoutPicker(ComboBox):
    """以小图标、方向文字和箭头显示当前结构，展开后提供独立勾选标记。

    保留组合框的值模型、activated 信号及无障碍接口；程序同步只改变展示。
    菜单关闭后拒绝迟到动作，取消、悬停和配色更新均不发送用户选择信号。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName("布局结构")
        # 与数值输入保持同高，固定逻辑尺寸由 Qt 按设备像素比缩放。
        self.setFixedSize(120, 34)
        self.popup_menu = SettingsMenu("布局结构", self, compact=True)
        self.popup_menu.setObjectName("layout_structure_popup")
        self.popup_menu.setFixedWidth(144)
        self.popup_menu.aboutToHide.connect(self._popup_closed)
        self._actions = []
        self._active = False
        self._popup_epoch = 0
        group = QActionGroup(self.popup_menu)
        group.setExclusive(True)
        for text, value in (("双向", "both"), ("向右", "right"), ("向左", "left"), ("向下", "down")):
            self.addItem(text, value)
            action = self.popup_menu.addAction(text)
            action.setData(value)
            action.setCheckable(True)
            group.addAction(action)
            self._actions.append(action)
            action.triggered.connect(lambda checked=False, selected=value: self._choose(selected))
        self.currentIndexChanged.connect(self._describe)
        self.set_theme(Theme())

    def _describe(self, *_):
        self.setAccessibleDescription("当前布局：" + self.currentText())
        for action in self._actions:
            action.setChecked(action.data() == self.currentData())
            font = action.font()
            font.setBold(action.isChecked())
            action.setFont(font)
        self.update()

    def set_value(self, value) -> None:
        """同步正式结构及勾选；即使调用方阻断 Qt 信号，也不留下旧的菜单状态。"""
        self.hidePopup()
        self.setCurrentIndex(self.findData(value))
        self._describe()

    def set_theme(self, theme) -> None:
        """刷新入口及弹层的独立色板，不修改模型当前值或文档配置。"""
        self.hidePopup()
        self._colors = ui_colors(theme)
        self.popup_menu.set_colors(self._colors)
        for index, action in enumerate(self._actions):
            icon = _layout_icon(action.data(), self._colors)
            self.setItemIcon(index, icon)
            action.setIcon(icon)
        self.update()

    def showPopup(self):
        """从正式值模型同步能力和勾选，再交给 Qt 处理屏幕避让及键盘导航。"""
        if not self.isEnabled():
            return
        for index, action in enumerate(self._actions):
            action.setEnabled(self.model().item(index).isEnabled())
        self._describe()
        self._popup_epoch += 1
        self._active = True
        self.popup_menu.ensurePolished()
        self.popup_menu.adjustSize()
        # 右边缘对齐入口，较宽的菜单向侧栏内展开；屏幕边缘避让仍由 Qt 负责。
        self.popup_menu.popup(self.mapToGlobal(QPoint(self.width() - self.popup_menu.width(), self.height() + 4)))
        current = next((action for action in self._actions if action.isChecked()), None)
        self.popup_menu.setActiveAction(current)

    def hidePopup(self):
        menu = getattr(self, "popup_menu", None)
        if menu is not None:
            self._active = False
            menu.close()
        super().hidePopup()

    def _popup_closed(self):
        # Qt 真实点击先隐藏菜单，再发 triggered；本轮结束后才使正常选择失效。
        # 显式 hidePopup 则立即失效，文档切换后的旧动作不能再次写回。
        epoch = self._popup_epoch

        def finish():
            if epoch == self._popup_epoch:
                self._active = False

        QTimer.singleShot(0, self, finish)
        super().hidePopup()
        self.update()

    def _choose(self, value):
        if not self.isEnabled() or not self._active:
            return
        index = self.findData(value)
        if index < 0 or not self.model().item(index).isEnabled():
            return
        self.hidePopup()
        self.setCurrentIndex(index)
        self.activated.emit(index)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter,
                           Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.showPopup()
            event.accept()
        else:
            super().keyPressEvent(event)

    def hideEvent(self, event):
        self.hidePopup()
        super().hideEvent(event)

    def sizeHint(self):
        return QSize(120, 34)

    def minimumSizeHint(self):
        return QSize(120, 34)

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, event):
        """按固定图文列绘制紧凑入口，显示当前方向，保留清晰的焦点和悬停反馈。"""
        colors = self._colors
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        focused = self.hasFocus() or self.popup_menu.isVisible()
        painter.setPen(QPen(QColor(colors.accent if focused else colors.border), 1))
        painter.setBrush(QColor(colors.hover if self.isEnabled() and self.underMouse() else colors.field))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
        mode = QIcon.Mode.Normal if self.isEnabled() else QIcon.Mode.Disabled
        self.itemIcon(self.currentIndex()).paint(
            painter, QRect(10, (self.height() - 16) // 2, 24, 16),
            Qt.AlignmentFlag.AlignCenter, mode,
        )
        painter.setPen(QColor(colors.text if self.isEnabled() else colors.muted))
        painter.setFont(self.font())
        painter.drawText(QRect(42, 0, self.width() - 66, self.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.currentText())
        x, y = self.width() - 14, self.height() / 2
        painter.setPen(QPen(QColor(colors.text if self.isEnabled() else colors.muted), 1.8))
        painter.drawPolyline(QPolygonF([QPointF(x - 4, y - 2), QPointF(x, y + 2), QPointF(x + 4, y - 2)]))


class SpacingSpinBox(QDoubleSpinBox):
    """保留一位小数和输入法合成状态；提交时机由业务面板统一管理。

    composing 为只读输入状态，composition_finished 在候选结束后通知面板恢复
    防抖。无焦点时滚轮交给侧栏，防止浏览设置时误改导图。
    """

    composition_finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._composing = False
        self.setDecimals(1)
        self.setSingleStep(1)
        self.setMinimumSize(80, 34)
        self.setMaximumWidth(120)
        self.installEventFilter(self)
        self.lineEdit().installEventFilter(self)

    @property
    def composing(self) -> bool:
        """返回当前控件是否仍有未确认的输入法候选，查询不提交文本。"""
        return self._composing

    def discard_composition(self) -> None:
        """丢弃本字段的过期候选，不提交文字，也不影响其他控件的输入法。

        文档替换、权限变化及组件释放后调用；空输入法事件只清除内部编辑器的
        预编辑文本，避免 setValue 重置显示后仍被旧合成标记永久阻止保存。
        """
        if self._composing:
            self._composing = False
            QApplication.sendEvent(self.lineEdit(), QInputMethodEvent())

    def eventFilter(self, watched, event):
        # QAbstractSpinBox 会将输入法事件转交内部 QLineEdit，必须在转交前跟踪
        # 两个对象的事件，而不能仅重写 QWidget.inputMethodEvent。
        if watched in (self, self.lineEdit()) and event.type() == QEvent.Type.InputMethod:
            previous = self._composing
            self._composing = bool(event.preeditString())
            if previous and not self._composing:
                QTimer.singleShot(0, self, self.composition_finished.emit)
        return super().eventFilter(watched, event)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()

    def textFromValue(self, value):
        return super().textFromValue(value).removesuffix(self.locale().decimalPoint() + "0")

    def paintEvent(self, event):
        """显式绘制上下箭头，保持原生步进、长按与高 DPI 的命中行为。"""
        super().paintEvent(event)
        option = QStyleOptionSpinBox()
        self.initStyleOption(option)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        for control, up in ((QStyle.SubControl.SC_SpinBoxUp, True),
                            (QStyle.SubControl.SC_SpinBoxDown, False)):
            rect = self.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option, control, self)
            enabled = self.isEnabled() and (self.value() < self.maximum() if up else self.value() > self.minimum())
            group = QPalette.ColorGroup.Active if enabled else QPalette.ColorGroup.Disabled
            painter.setBrush(self.palette().color(group, QPalette.ColorRole.Text))
            center, sign = rect.center(), -1 if up else 1
            painter.drawPolygon(QPolygonF([
                QPointF(center.x() - 3, center.y() - sign * 1.5),
                QPointF(center.x() + 3, center.y() - sign * 1.5),
                QPointF(center.x(), center.y() + sign * 2),
            ]))
