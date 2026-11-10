"""大纲和收藏共用的树形外观，模型和导航由各面板管理。"""

from __future__ import annotations

from math import ceil, floor

from mindmap_qt_ui.ui_style import UiColors, apply_ui_palette, scrollbar_styles
from PySide6.QtCore import QEvent, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QAbstractItemView, QTreeView

# 所有几何尺寸使用 Qt 逻辑像素，由 Qt 统一适配系统缩放。条目右侧留白同时
# 用于样式表和收藏的键盘焦点描边，避免描边越过条目底色的边界。
_INDENTATION = 16
_ROW_HEIGHT = 28
_FONT_SIZE = 12
ITEM_RIGHT_MARGIN = 8


class SidebarTreeView(QTreeView):
    """提供统一的行和分支样式，展开命中、选择与键盘输入沿用原生树。"""

    def __init__(self, parent=None):
        """设置通用行几何与视口行为，调用方负责安装模型并提供界面色板。"""
        super().__init__(parent)
        self._tree_colors: UiColors | None = None
        self._branch_hover_position = None
        self.setHeaderHidden(True)
        self.setUniformRowHeights(True)
        self.setIndentation(_INDENTATION)
        font = self.font()
        font.setPixelSize(_FONT_SIZE)
        self.setFont(font)
        self.setRootIsDecorated(True)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

    def set_tree_colors(self, colors: UiColors) -> bool:
        """应用侧栏底色、条目状态、分支和滚动条配色，返回色板是否变化。

        调用方传入与外层卡片一致的色板，并按返回值同步自己的标题操作或菜单。
        色板只作用于当前树视图，重复应用同一色板时跳过重设样式，保留浏览现场。
        """
        if self._tree_colors == colors:
            return False
        self._tree_colors = colors
        dark = QColor(colors.surface).lightnessF() < 0.5
        self._guide_color = QColor("#45474b" if dark else "#dfe2e8")
        self._marker_color = QColor(colors.muted)
        self.setStyleSheet(
            f"QTreeView {{ background: {colors.surface}; color: {colors.text}; border: none; "
            "padding: 5px 7px; outline: none; }"
            f"QTreeView::item {{ height: {_ROW_HEIGHT}px; border: none; border-radius: 4px; "
            f"padding: 0 3px; margin-right: {ITEM_RIGHT_MARGIN}px; }}"
            f"QTreeView::item:hover {{ background: {colors.hover}; }}"
            f"QTreeView::item:selected {{ background: {colors.selected}; color: {colors.text}; }}"
            + scrollbar_styles(colors)
        )
        apply_ui_palette(self, colors)
        self.viewport().update()
        return True

    def drawBranches(self, painter, rect, index):
        """在 Qt 给出的整行分支区域绘制祖先竖线和本行标记。

        分支区域先铺面板底色，避免条目高亮延伸到缩进列。竖线贯穿各祖先列，
        延续到末尾子项的下边缘。本行最后一级按模型的子项状态绘制三角或圆点，
        标记中心由 Qt 的分支矩形计算，随布局方向和滚动同步移动。
        """
        colors = self._tree_colors
        indentation = self.indentation()
        if colors is None or not index.isValid() or indentation <= 0 or rect.isEmpty():
            return
        painter.save()
        try:
            painter.fillRect(rect, QColor(colors.surface))
            rtl = self.isRightToLeft()
            marker_x = rect.left() + indentation / 2 if rtl else rect.right() + 1 - indentation / 2
            marker_y = rect.top() + rect.height() / 2
            ancestors = max(0, rect.width() // indentation - 1)
            first_x = marker_x + indentation if rtl else rect.left() + indentation / 2

            # 深层树的分支矩形可能远宽于视口。先裁剪可见列号，使开销只与视口
            # 内的导线数量有关，无需遍历离屏祖先或访问模型的父级链。
            visible = rect.intersected(self.viewport().rect())
            first = max(0, ceil((visible.left() - first_x) / indentation))
            stop = min(ancestors, floor((visible.right() - first_x) / indentation) + 1)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            # 先取整设备像素再换回逻辑线宽，防止 150% 缩放时竖线被光栅器截细。
            ratio = painter.device().devicePixelRatioF()
            guide_width = max(1, floor(ratio + 0.5)) / ratio
            painter.setPen(QPen(self._guide_color, guide_width))
            for column in range(first, stop):
                x = first_x + column * indentation
                painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom() + 1))

            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._marker_color)
            if self.model().hasChildren(index):
                hover = self._branch_hover_position
                hovered = (
                    hover is not None
                    and rect.contains(hover)
                    and abs(hover.x() - marker_x) < indentation / 2
                )
                if self.isExpanded(index):
                    points = ((-3.5, -2.5), (3.5, -2.5), (0, 3)) if hovered else (
                        (-2.5, -1.5), (2.5, -1.5), (0, 2)
                    )
                else:
                    direction = -1 if rtl else 1
                    points = (
                        ((-2.5 * direction, -3.5), (-2.5 * direction, 3.5), (3 * direction, 0))
                        if hovered else
                        ((-1.5 * direction, -2.5), (-1.5 * direction, 2.5), (2 * direction, 0))
                    )
                painter.drawPolygon(QPolygonF([QPointF(marker_x + x, marker_y + y) for x, y in points]))
            else:
                painter.drawEllipse(QPointF(marker_x, marker_y), 2, 2)
        finally:
            painter.restore()

    def viewportEvent(self, event):
        """按视口坐标记录分支悬停，重绘新旧两行后交回 Qt 处理原生事件。

        滚动或重排后，鼠标仍位于同一屏幕位置，重绘时用当前行几何判断命中，
        避免把已经移动或失效的模型索引当作悬停目标。
        """
        if event.type() in (QEvent.Type.MouseMove, QEvent.Type.Leave):
            previous = self._branch_hover_position
            self._branch_hover_position = (
                event.position().toPoint() if event.type() == QEvent.Type.MouseMove else None
            )
            if previous != self._branch_hover_position:
                for position in (previous, self._branch_hover_position):
                    if position is not None:
                        rect = self.visualRect(self.indexAt(position))
                        if rect.isValid():
                            self.viewport().update(QRect(0, rect.top(), self.viewport().width(), rect.height()))
        return super().viewportEvent(event)
