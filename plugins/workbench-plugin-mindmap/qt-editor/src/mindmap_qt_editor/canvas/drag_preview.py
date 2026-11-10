"""绘制画布拖动影像和落点提示，由拖动控制器管理图层生命周期。"""

from math import ceil

from mindmap_qt_render.rendering import paint_branch
from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem

from .drop_targets import _qrect


class _DragLayer(QGraphicsItem):
    """只绘制源分支影像及落点提示；没有鼠标或键盘能力，不参与节点命中。"""

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self._bounds = QRectF()
        self._label_bounds = QRectF()
        self._label_sizes = {}
        self._label_font = QFont(controller.view.font())
        self._label_font.setPixelSize(12)
        self._label_font.setWeight(QFont.Weight.DemiBold)
        self._label_metrics = QFontMetricsF(self._label_font)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setZValue(40)

    def boundingRect(self):
        return self._bounds

    def sync(self):
        control = self.controller
        bounds = control._ghost_bounds.translated(control._offset)
        if control.target is not None:
            rect = control._base.layout.nodes.get(control.target.reference)
            if rect is not None:
                bounds = bounds.united(_qrect(rect))
            bounds = bounds.united(control._connection_hint().boundingRect())
            bounds = bounds.united(control._insertion_hint())
        self._sync_label()
        bounds = bounds.united(self._label_bounds)
        # 提示线使用屏幕固定线宽，低倍率时把整条提示线的场景留白计入重绘范围。
        padding = max(4, 12 / control.view.transform().m11())
        self.prepareGeometryChange()
        self._bounds = bounds.adjusted(-padding, -padding, padding, padding)
        self.update()

    def _sync_label(self):
        """按视口逻辑像素放置文字气泡，贴近鼠标并在视口边缘翻向，始终保持可读。

        气泡仅属于绘制图层，不是可命中的按钮；大小不随导图缩放改变。命中仍由
        固定节点和节点外间隙决定，避免气泡覆盖画面时反过来改变当前落点。
        """
        control = self.controller
        label = control._hint_text()
        if not label:
            self._label_bounds = QRectF()
            return
        if label not in self._label_sizes:
            self._label_sizes[label] = (
                ceil(self._label_metrics.horizontalAdvance(label)) + 20,
                ceil(self._label_metrics.height()) + 12,
            )
        width, height = self._label_sizes[label]
        viewport = control.view.viewport().rect()
        point = control._pointer
        left, top = point.x() + 16, point.y() + 20
        if left + width > viewport.right() - 8:
            left = point.x() - width - 16
        if top + height > viewport.bottom() - 8:
            top = point.y() - height - 16
        left = max(8, min(left, viewport.right() - width - 8))
        top = max(8, min(top, viewport.bottom() - height - 8))
        scale = control.view.transform().m11()
        origin = control.view.mapToScene(QPoint(left, top))
        self._label_bounds = QRectF(origin.x(), origin.y(), width / scale, height / scale)

    def paint(self, painter, option, widget=None):
        control = self.controller
        painter.save()
        # 吸附后降低影像浓度，避免源分支遮住正在选择的目标文字。
        painter.setOpacity(0.45 if control.target else 0.72)
        painter.translate(control._offset)
        # 侧栏源树没有原画布的视口裁剪，预先缓存绘制边界，只画当前视口中的影像。
        visible = control.view.mapToScene(control.view.viewport().rect()).boundingRect().translated(-control._offset)
        padding = 4 / control.view.transform().m11()
        visible.adjust(-padding, -padding, padding, padding)
        for branch, bounds in zip(control._ghost_branches, control._ghost_branch_bounds):
            if bounds.intersects(visible):
                paint_branch(painter, branch)
        for key, rect in control._ghost_nodes.items():
            if not control._ghost_node_bounds[key].intersects(visible):
                continue
            painter.save()
            painter.translate(rect.x, rect.y)
            control._source.visuals[key].paint(painter, control._source.document.nodes[key], controls=False)
            painter.restore()
        painter.restore()
        target = control.target
        rect = control._base.layout.nodes.get(target.reference) if target else None
        if rect is None:
            self._paint_free_label(painter)
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(control._base.document.theme.accent)
        pen = QPen(color, 2)
        pen.setCosmetic(True)
        if target.position == "inside":
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(control._connection_hint())
        # 影像可能正好盖住落点正文。把固定目标绘制在影像与提示线之上，使名称和
        # 正文始终清晰；预览不绘制交互按钮，也不移动或改写实际节点图元。
        painter.save()
        painter.translate(rect.x, rect.y)
        # 虚拟画布滚动后可能命中起点视口之外的节点，复用正式测量，避免拖动中重新排版。
        visual = control._base.visuals.get(target.reference) or control.owner._visuals[target.reference]
        visual.paint(
            painter, control._base.document.nodes[target.reference], controls=False
        )
        painter.restore()
        if target.position == "inside":
            # 接为子节点强调接收主题，整圈淡色高亮与预连接共同表达父子关系。
            pen.setStyle(Qt.PenStyle.SolidLine)
            painter.setPen(pen)
            fill = QColor(color)
            fill.setAlpha(30)
            painter.setBrush(fill)
            painter.drawRoundedRect(_qrect(rect).adjusted(-3, -3, 3, 3), 8, 8)
        else:
            # 同级排序只标出插入位置，不叠加目标框或预连接；实心圆角短条与正文
            # 留出空隙，避免旧的长线贴边形成双层边框。全部提示均不参与命中。
            marker = control._insertion_hint()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            radius = min(marker.width(), marker.height()) / 2
            painter.drawRoundedRect(marker, radius, radius)
        painter.restore()
        # 用反向倍率绘制固定字号的文字与气泡；标签给出松手后的准确操作，不能
        # 随 10%～400% 导图缩放变成不可读的小字或遮挡大片内容的大字。
        painter.save()
        painter.translate(self._label_bounds.topLeft())
        scale = control.view.transform().m11()
        painter.scale(1 / scale, 1 / scale)
        badge = QRectF(0, 0, self._label_bounds.width() * scale, self._label_bounds.height() * scale)
        painter.setPen(QPen(color, 1))
        painter.setBrush(QColor(control._base.document.theme.background))
        painter.drawRoundedRect(badge, 7, 7)
        painter.setFont(self._label_font)
        painter.setPen(color)
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, control._hint_text())
        painter.restore()


    def _paint_free_label(self, painter):
        """空白落点仅绘制独立主题提示，不把源主题高亮成结构目标。"""
        if self._label_bounds.isEmpty():
            return
        control = self.controller
        color = QColor(control._base.document.theme.accent)
        painter.save()
        painter.translate(self._label_bounds.topLeft())
        scale = control.view.transform().m11()
        painter.scale(1 / scale, 1 / scale)
        badge = QRectF(0, 0, self._label_bounds.width() * scale, self._label_bounds.height() * scale)
        painter.setPen(QPen(color, 1))
        painter.setBrush(QColor(control._base.document.theme.background))
        painter.drawRoundedRect(badge, 7, 7)
        painter.setFont(self._label_font)
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, control._hint_text())
        painter.restore()
