"""静态节点图元；只负责绘制与命中轮廓，不持有编辑器或交互宿主。"""

from PySide6.QtCore import QRectF
from PySide6.QtGui import QPainterPath
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject


class NodeItem(QGraphicsObject):
    """显示静态节点；折叠标记和页面图标分别控制，不注册编辑或文档修改行为。"""

    def __init__(self, node, visual, theme, *, controls=False, page_controls=True):
        """借用节点及只读视觉；page_controls 默认绘制已计入尺寸的页面图标。

        controls 沿用既有的外侧折叠标记开关；交互画布另有页面按钮子图元，
        因而将 page_controls 关闭以免重复绘制。两个开关都不改变节点测量。
        """
        super().__init__()
        self.node, self.visual, self.theme = node, visual, theme
        self.controls = bool(controls)
        self.page_controls = bool(page_controls)
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
        """返回当前视觉的命中路径；纯图片为矩形，普通节点为圆角矩形。

        无参数，不修改文档，正常调用无异常。返回独立 QPainterPath，由 Qt 在
        GUI 线程、图元存活期间查询，不将透明图片的内部像素作为命中空洞。
        """
        path = QPainterPath()
        rect = QRectF(0, 0, self.visual.width, self.visual.height)
        if self.node.is_image_node:
            path.addRect(rect)
        else:
            path.addRoundedRect(rect, 7, 7)
        return path

    def paint(self, painter, option, widget=None):
        self.visual.paint(
            painter,
            self.node,
            selected=self.isSelected(),
            editing=self.editing,
            controls=self.controls,
            page_controls=self.page_controls,
            accent=self.theme.accent,
        )
