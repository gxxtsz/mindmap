"""纯图片缩放手势：视图前景绘制手柄和预览，松手才修改正式文档。

预览不改动节点尺寸、布局或资源，不创建额外场景图元。手柄使用视口逻辑坐标，
与画布缩放无关；所有状态仅由所属画布持有，取消、切页和释放时立即清理。
"""

from math import isclose, isfinite

from mindmap_core import MindMapError
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPen
from shiboken6 import isValid

from .image_decoration import paint_image_shadow
from .items import NodeItem

# 顺时针排列八个边缘手柄，零分量表示以对应边的中点为锚点。
HANDLES = ((-1, -1), (0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0))
_CURSORS = (
    Qt.CursorShape.SizeFDiagCursor, Qt.CursorShape.SizeVerCursor,
    Qt.CursorShape.SizeBDiagCursor, Qt.CursorShape.SizeHorCursor,
    Qt.CursorShape.SizeFDiagCursor, Qt.CursorShape.SizeVerCursor,
    Qt.CursorShape.SizeBDiagCursor, Qt.CursorShape.SizeHorCursor,
)


class ImageResizeController:
    """管理单个视图的纯图片缩放，不共享会话或持有系统剪贴板对象。

    view 为存活的 MindMapView；所有方法仅在 GUI 线程同步使用。控制器由视图拥有，
    不能跨视图复用；宿主释放前须调用 cancel()，归还预览借用的图元及视觉。
    """

    def __init__(self, view):
        self.view = view
        self._item = None
        self._visual = None
        self._rect = None
        self._pointer = None

    @property
    def active(self) -> bool:
        """返回是否正在预览；无参数、无副作用，无正常业务异常。"""
        return self._item is not None

    def _selected_item(self):
        owner = self.view.owner
        if (owner._disposed or not owner.editable or owner.editor is not None
                or owner._applying_engine_event or owner._refreshing):
            return None
        # 文档/页面替换时，场景可能暂时还保留旧选择。这里仅判断单选，不借用
        # 新文档的树顺序索引排序旧 ID，也不在鼠标移动时重建完整节点顺序。
        selected = [item for item in owner.scene.selectedItems() if isinstance(item, NodeItem)]
        if len(selected) != 1:
            return None
        item = selected[0]
        node = owner._document.nodes.get(item.node.id)
        return item if node is not None and node.is_image_node else None

    def _viewport_rect(self):
        item = self._item if self.active else self._selected_item()
        if item is None:
            return None
        rect = self._rect if self.active else item.mapRectToScene(item.visual.image_rect)
        return self.view.viewportTransform().mapRect(rect)

    @staticmethod
    def _centers(rect):
        return [QPointF(rect.center().x() + x * rect.width() / 2,
                        rect.center().y() + y * rect.height() / 2) for x, y in HANDLES]

    def hit_test(self, position):
        """按视口坐标返回命中的手柄下标，无命中返回 None；不修改选择或输入。

        position 为有效视口点，命中区固定为 12×12 逻辑像素，小图重叠时选择最近
        中心。只读、多选及正文编辑中无手柄；只在 GUI 线程、视图存活时调用。
        """
        rect = self._viewport_rect()
        if rect is None or position is None:
            return None
        # 使用单独候选列表，不能依赖图元 ItemAt 的缩放后边界。
        candidates = [((QPointF(position) - center).manhattanLength(), index)
                      for index, center in enumerate(self._centers(rect))
                      if QRectF(center.x() - 6, center.y() - 6, 12, 12).contains(QPointF(position))]
        return min(candidates)[1] if candidates else None

    def cursor_at(self, position):
        """返回手柄的 Qt 缩放光标，非手柄区域返回 None；仅查询，不抓取鼠标。"""
        index = self._handle if self.active else self.hit_test(position)
        return _CURSORS[index] if index is not None else None

    def begin(self, index: int, position) -> None:
        """验证输入后开始预览，返回 None；失败抛出 MindMapError 且不隐藏原图。

        index 是 HANDLES 下标，position 为按下时的视口点。有效属性草稿先独立
        提交，再冻结正式尺寸、修订号和图元；输入法候选不会被强制提交或丢弃。
        """
        owner = self.view.owner
        item = self._selected_item()
        if item is None or index not in range(len(HANDLES)):
            raise MindMapError("请选择一个可编辑的纯图片节点")
        key, generation = item.node.id, owner.engine.document_generation
        if owner.is_composing or not all(guard() for guard in tuple(owner._navigation_guards)):
            raise MindMapError("请先完成输入法候选，再调整图片尺寸")
        owner.edits_flush_requested.emit()
        item = self._selected_item()
        if (item is None or item.node.id != key or generation != owner.engine.document_generation):
            raise MindMapError("调整图片前导图或选择已发生变化，请重新操作")
        self._handle = index
        self._generation = generation
        self._revision = owner.engine.revision
        self._page_id = owner.current_page_id
        self._item, self._visual = item, item.visual
        self._base = item.mapRectToScene(item.visual.image_rect)
        self._rect = QRectF(self._base)
        self._press = self.view.mapToScene(position)
        self._pointer = position
        self._opacity = item.opacity()
        item.setOpacity(0)
        self.view.viewport().update()

    def _valid(self):
        owner = self.view.owner
        return (self.active and isValid(self._item) and not owner._disposed and owner.editable
                and self._generation == owner.engine.document_generation
                and self._revision == owner.engine.revision and self._page_id == owner.current_page_id)

    def update(self, position) -> None:
        """按起点矩形等比更新预览；position 是视口点，不写文档或历史。

        最短边至少一个逻辑像素，角手柄使用起始对角线的投影，边手柄使用对应
        坐标轴；不会被预览几何反向影响。状态失效时自动取消，无正常业务异常。
        """
        if not self._valid():
            self.cancel()
            return
        delta = self.view.mapToScene(position) - self._press
        x, y = HANDLES[self._handle]
        width, height = self._base.width(), self._base.height()
        if x and y:
            scale = 1 + (delta.x() * x * width + delta.y() * y * height) / (width ** 2 + height ** 2)
        else:
            scale = 1 + (delta.x() * x / width if x else delta.y() * y / height)
        if not isfinite(scale):
            return
        scale = max(1 / min(width, height), scale)
        w, h = width * scale, height * scale
        left = self._base.right() - w if x < 0 else self._base.left() if x else self._base.center().x() - w / 2
        top = self._base.bottom() - h if y < 0 else self._base.top() if y else self._base.center().y() - h / 2
        self._rect = QRectF(left, top, w, h)
        self._pointer = position
        self.view.viewport().update()

    def finish(self, position) -> None:
        """结束手势并一次性提交显示宽度，返回 None；空拖动不产生历史。

        position 为松手视口点；先归还图元再执行引擎命令，避免同步刷新访问旧
        预览。输入法、只读或事务错误抛出 MindMapError，界面入口应通过 call()
        报告；不重采样图片，也不发出导入信号。失效手势只取消、不提交。
        """
        self.update(position)
        if not self.active:
            return
        key, width, old_width = self._item.node.id, self._rect.width(), self._base.width()
        self.cancel()
        owner = self.view.owner
        if owner.is_composing or not all(guard() for guard in tuple(owner._navigation_guards)):
            raise MindMapError("请先完成输入法候选，再调整图片尺寸")
        if not isclose(width, old_width, rel_tol=1e-9, abs_tol=1e-7):
            owner.engine.update_node(key, image_width=width)

    def cancel(self) -> None:
        """幂等丢弃预览、恢复原图，返回 None，不提交文档、草稿或历史。

        切页、文档变化、鼠标捕获中断和视图释放前均调用；Qt 图元已销毁时只
        清理引用，不再访问 C++ 对象。只在所属 GUI 线程、视图仍存活时使用。
        """
        item, self._item = self._item, None
        self._visual = self._rect = self._pointer = None
        if item is not None and isValid(item):
            item.setOpacity(self._opacity)
        if item is not None:
            self.view.viewport().update()

    def paint(self, painter) -> None:
        """绘制视图专用前景，返回 None；painter 仅在本次 GUI 绘制期间借用。

        正式导出使用场景而非视图，因此不含手柄或预览；保存并恢复画笔状态，
        不修改共享视觉或资源，不缓存 painter，正常调用无业务异常。
        """
        rect = self._viewport_rect()
        if rect is None:
            return
        painter.save()
        if self.active:
            paint_image_shadow(painter, self._rect, self.view.scene().backgroundBrush().color())
            painter.drawPixmap(self._rect, self._visual.image, QRectF(self._visual.image.rect()))
        painter.resetTransform()
        accent = QColor(self.view.owner._document.theme.accent)
        painter.setPen(QPen(accent, 1.5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)
        painter.setBrush(QColor("#ffffff"))
        for center in self._centers(rect):
            painter.drawRect(QRectF(center.x() - 4, center.y() - 4, 8, 8))
        if self.active:
            label = f"{self._rect.width():.1f} × {self._rect.height():.1f}"
            metrics = painter.fontMetrics()
            w, h = metrics.horizontalAdvance(label) + 16, metrics.height() + 8
            x = max(0, min(self._pointer.x() + 14, self.view.viewport().width() - w))
            y = max(0, min(self._pointer.y() + 14, self.view.viewport().height() - h))
            box = QRectF(x, y, w, h)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#253044"))
            painter.drawRoundedRect(box, 4, 4)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, label)
        painter.restore()
