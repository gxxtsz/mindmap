"""导图设置菜单的局部绘制：已选项与临时悬停分别使用蓝色和中性灰色。"""

from dataclasses import replace

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QMenu

from .ui_style import UiColors, apply_ui_palette, menu_styles


class SettingsMenu(QMenu):
    """保留 QMenu 原生动作、键盘导航、子菜单和无障碍语义，只替换菜单行绘制。

    原生 Windows 菜单会让左侧图标占据勾选位置，当前配置因而只剩图标变色。
    这里将图标、文字和右侧状态标记分列绘制，checked 始终决定固定高亮，
    activeAction 只表示鼠标悬停或键盘定位，不参与判断配置是否已选中。
    """

    def __init__(self, *args, compact: bool = False, **kwargs):
        """保留 QMenu 构造方式，compact 仅为布局选择器启用紧凑菜单。

        普通设置菜单保持原有尺寸；紧凑菜单采用较小行高和独立图文间距，
        并关闭系统投影，避免短选项菜单在侧栏外形成大面积阴影。
        """
        super().__init__(*args, **kwargs)
        self._compact = compact
        if compact:
            self.setWindowFlags(self.windowFlags() | Qt.WindowType.NoDropShadowWindowHint)

    def set_colors(self, colors: UiColors) -> None:
        """依据宿主界面底色适配明暗主题；本地色板不影响导图主题或其他菜单。"""
        dark = QColor(colors.surface).lightnessF() < 0.5
        self._colors = replace(
            colors,
            selected="#29466c" if dark else "#dce9ff",
            hover="#363a42" if dark else "#f0f1f3",
            accent="#93c5fd" if dark else "#2563eb",
        )
        # 预留左侧图标和右侧勾选／箭头空间，Qt 仍负责菜单几何、屏幕避让和命中。
        self.setStyleSheet(
            menu_styles(self._colors)
            + ("QMenu::item { height: 28px; min-height: 28px; padding: 0 28px 0 40px; }"
               if self._compact else "QMenu::item { padding: 8px 38px 8px 44px; min-height: 18px; }")
        )
        apply_ui_palette(self, self._colors)
        self.update()

    def paintEvent(self, event):
        """先交给 Qt 绘制边框等框架，再覆盖普通菜单行，避免系统勾选图形与图标重叠。"""
        super().paintEvent(event)
        if not hasattr(self, "_colors"):
            return
        colors = self._colors
        painter = QPainter(self)
        painter.setClipRegion(event.region())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for action in self.actions():
            rect = self.actionGeometry(action)
            if not action.isVisible() or action.isSeparator() or not event.region().intersects(rect):
                continue
            checked = action.isCheckable() and action.isChecked()
            active = action.isEnabled() and self.activeAction() is action
            painter.fillRect(rect, QColor(colors.surface))
            row = QRectF(rect).adjusted(2, 1, -2, -1)
            background = colors.selected if checked else colors.hover if active else colors.surface
            painter.setPen(QPen(QColor(colors.accent), 1) if checked and active else Qt.PenStyle.NoPen)
            painter.setBrush(QColor(background))
            painter.drawRoundedRect(row, 4, 4)
            foreground = QColor(colors.text if action.isEnabled() else colors.muted)
            marker = QColor(colors.accent if action.isEnabled() else colors.muted)
            if checked and not self._compact:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(marker)
                painter.drawRoundedRect(QRectF(rect.left() + 5, rect.top() + 7, 3, rect.height() - 14), 1.5, 1.5)
            if not action.icon().isNull():
                mode = QIcon.Mode.Normal if action.isEnabled() else QIcon.Mode.Disabled
                action.icon().paint(
                    painter,
                    QRect(rect.left() + 10, rect.center().y() - 8, 24, 16) if self._compact
                    else QRect(rect.left() + 14, rect.center().y() - 10, 20, 20),
                    Qt.AlignmentFlag.AlignCenter, mode, QIcon.State.Off,
                )
            font = QFont(action.font())
            font.setBold(checked)
            painter.setFont(font)
            painter.setPen(foreground)
            text_rect = rect.adjusted(40, 0, -28, 0) if self._compact else rect.adjusted(44, 0, -36, 0)
            text = painter.fontMetrics().elidedText(action.text(), Qt.TextElideMode.ElideRight, text_rect.width())
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
            # 使用矢量线段绘制勾选和箭头，不依赖字体是否包含特殊字符，高 DPI 下也清晰。
            x, y = rect.right() - (14 if self._compact else 20), rect.center().y()
            painter.setPen(QPen(marker if checked else foreground, 2,
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            if action.menu() is not None:
                points = (QPointF(x - 2, y - 4), QPointF(x + 2, y), QPointF(x - 2, y + 4))
                painter.drawPolyline(QPolygonF(points))
            elif checked:
                points = (QPointF(x - 5, y), QPointF(x - 1, y + 4), QPointF(x + 6, y - 4))
                painter.drawPolyline(QPolygonF(points))
        painter.end()
