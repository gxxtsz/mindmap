"""静态节点图元；只负责绘制与命中轮廓，不持有编辑器或交互宿主。"""

from PySide6.QtCore import QRectF
from PySide6.QtGui import QPainterPath
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject


class NodeItem(QGraphicsObject):
    """显示一个节点；controls 仅控制折叠标记绘制，不注册任何编辑或文档修改行为。"""

    def __init__(self, node, visual, theme, *, controls=False):
        super().__init__()
        self.node, self.visual, self.theme = node, visual, theme
        self.controls = bool(controls)
        self.editing = False
        self.setZValue(5)
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable | QGraphicsItem.GraphicsItemFlag.ItemIsFocusable
        )
        # 节点正文、折叠按钮、只读状态的光标由视图按命中位置统一判断。
        self.setToolTip(node.note or node.hyperlink)

    def update_content(self, node, visual, theme):
        """替换借用的节点及视觉，尺寸变化时通知 Qt 更新空间索引。

        本方法只同步展示，不修改节点、文档或布局；调用方负责保证参数来自同一
        正式快照，并在 GUI 线程调用，以保留图元选择及场景中的对象身份。
        """
        # 文本、折叠状态或颜色变化不一定改变尺寸；仅在边界变化时使场景空间索引失效。
        size_changed = (self.visual.width, self.visual.height) != (visual.width, visual.height)
        control_changed = self.controls and (
            (len(self.node.children), self.node.expanded) != (len(node.children), node.expanded)
        )
        if size_changed or control_changed:
            self.prepareGeometryChange()
        self.node, self.visual, self.theme = node, visual, theme
        self.setToolTip(node.note or node.hyperlink)
        self.update()

    def boundingRect(self):
        """静态控件调用覆盖外侧标记；无控件时只返回节点正文边界。"""
        bounds = QRectF(-2, -2, self.visual.width + 4, self.visual.height + 4)
        if self.controls and self.node.children:
            control = self.visual.fold_rect(child_count=0 if self.node.expanded else len(self.node.children))
            bounds = bounds.united(control.adjusted(-1, -1, 1, 1))
        return bounds

    def shape(self):
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, self.visual.width, self.visual.height), 7, 7)
        return path

    def paint(self, painter, option, widget=None):
        self.visual.paint(
            painter,
            self.node,
            selected=self.isSelected(),
            editing=self.editing,
            controls=self.controls,
            accent=self.theme.accent,
        )
