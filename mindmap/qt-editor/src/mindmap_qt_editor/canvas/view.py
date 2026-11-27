"""画布事件路由：平移、缩放、节点拖拽和键盘空间导航。"""

from contextlib import contextmanager
from math import ceil
from pathlib import Path
from time import monotonic
from weakref import ref

from mindmap_qt_render.graph_groups import GraphGroupItem
from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QGraphicsView
from shiboken6 import isValid

from .dragging import NodeDragController
from .graph_edge_editing import GraphEdgeEditController
from .graph_group_resizing import GraphGroupResizeController
from .image_resizing import ImageResizeController
from .items import (
    FOLD_HIDE_DELAY,
    FOLD_HOVER_PADDING,
    FoldControlItem,
    NodeEditor,
    NodeItem,
    PageControlItem,
)
from .selection_decoration import SelectionDecoration, paint_node_selection, selection_bounds
from .text_resizing import TextWidthResizeController

# 交互缩放与“适应窗口”共用 10% 下限，避免自动适配绕过交互入口的倍率限制。
MIN_ZOOM = 0.1


class _GraphOcclusion:
    """按视口网格索引遮挡轮廓，只在本次命中或绘制期间使用。"""

    def __init__(self, area, records):
        self.area = QRectF(area)
        self.cells = {}
        for key, record in enumerate(records):
            _rank, bounds, _outline = record
            for cell in self._overlapping_cells(bounds):
                self.cells.setdefault(cell, {})[key] = record

    def _overlapping_cells(self, bounds):
        rect = self.area.intersected(bounds).toAlignedRect()
        if not rect.isEmpty():
            cell_size = 96
            for row in range(rect.top() // cell_size, rect.bottom() // cell_size + 1):
                for column in range(rect.left() // cell_size, rect.right() // cell_size + 1):
                    yield row, column

    def nearby(self, bounds):
        nearby = {}
        for cell in self._overlapping_cells(bounds):
            nearby.update(self.cells.get(cell, ()))
        return nearby.values()


class MindMapView(QGraphicsView):
    def _draw_inspiration_empty(self, painter):
        """空想法画布显示侧栏与双击新建指引；仅属于界面，不参与保存和导出。"""
        if self.owner.current_page_kind == "inspiration" and not self.owner.document_view.nodes:
            painter.save()
            painter.resetTransform()
            colors = self.owner._ui_colors
            painter.setPen(QColor(colors.muted if colors else "#788493"))
            painter.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter,
                             "先把想法记下来，稍后再确定归属\n\n双击空白处，或在左侧灵感栏点击“新建想法”")
            painter.restore()

    def __init__(self, owner, scene):
        super().__init__(scene, owner)
        self.owner = owner
        self._viewport_sync_depth = 0
        self.panning = False
        self._pan_button = Qt.MouseButton.NoButton
        self._pan_origin = None
        self._pointer_position = None
        self._hovered_node = None
        self._fold_hide_requests = {}
        self._fold_hide_timer = QTimer(self)
        self._fold_hide_timer.setSingleShot(True)
        self._fold_hide_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._fold_hide_timer.timeout.connect(self._expire_fold_hovers)
        self._fold_pressed = False
        self._wheel_scroll_remainder = 0.0
        self._hidden_center = None
        self.press_node = None
        self._selection_click = None
        self.drag_origin = QPointF()
        self.node_drag = NodeDragController(self)
        self._attachment_drag_file = None
        self._attachment_drop_node = None
        self.graph_edge_edit = GraphEdgeEditController(self)
        self.group_resize = GraphGroupResizeController(self)
        self.image_resize = ImageResizeController(self)
        self.text_resize = TextWidthResizeController(self)
        self._selection_bounds = QRect()
        self._pending_selection_bounds = QRect()
        self._selection_decoration = SelectionDecoration(self)
        self._selection_update_timer = QTimer(self)
        self._selection_update_timer.setSingleShot(True)
        self._selection_update_timer.timeout.connect(self._flush_selection_decoration)
        # 外框不属于节点边界；场景内容、草稿尺寸或动画位置变化后，额外清除
        # 前后两次的外框范围。只更新选中区域，避免为大图改用整视口持续重绘。
        scene.changed.connect(self._update_selection_decoration)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        # 只隐藏滚动条的界面，仍保留 Qt 的滚动范围和位移计算，供拖动、滚轮及
        # 键盘导航使用；这样缩放或内容扩展时也不会出现滚动条挤占视口的抖动。
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.BoundingRectViewportUpdate)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)

    @property
    def drag_ghost(self):
        """保留视图内部的预览查询入口，图元的创建和释放统一归拖动控制器管理。"""
        return self.node_drag.ghost

    @property
    def _active_resize(self):
        """借用当前认领鼠标的尺寸控制器，供移动和释放事件统一路由。"""
        if self.text_resize.active:
            return self.text_resize
        if self.group_resize.active:
            return self.group_resize
        return self.image_resize if self.image_resize.active else None

    def event(self, event):
        # 拖动期间先认领 Escape，保证 keyPressEvent 可以取消当前手势；即使宿主
        # 另行注册了同名快捷键，也不应在手势尚未取消时触发宿主动作或改变视图。
        if (
            event.type() == QEvent.Type.ShortcutOverride
            and event.key() == Qt.Key.Key_Escape
            and (self.press_node is not None or self._active_resize is not None
                 or self.node_drag.external_active or self.graph_edge_edit.active)
        ):
            event.accept()
            return True
        return super().event(event)

    def dragEnterEvent(self, event):
        """为有效原生拖入建立共用预览，空白处暂时接受进入以允许继续寻找落点。"""
        if event.mimeData().hasUrls():
            path = self._local_attachment_path(event)
            try:
                self._attachment_drag_file = path if path is not None and path.is_file() else None
            except (OSError, ValueError):
                self._attachment_drag_file = None
            if self._attachment_drag_file is None:
                self._set_attachment_drop_node(None)
                event.ignore()
            else:
                self._set_attachment_drop_node(self._attachment_node_at(event.position().toPoint()))
                event.setDropAction(Qt.DropAction.CopyAction)
                event.accept()
            return
        if self.owner.graph is not None:
            event.ignore()
            return
        accepted = self.owner.call(lambda: self.node_drag.begin_external(
            event.mimeData(), event.position().toPoint()))
        if accepted:
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            self.node_drag.cancel_external()
            event.ignore()

    def dragMoveEvent(self, event):
        """正文、同级间隙和吸附均交给节点控制器，预览不参与下一帧命中。"""
        if event.mimeData().hasUrls():
            copy_allowed = self._attachment_drag_file is not None and event.possibleActions() & Qt.DropAction.CopyAction
            node_id = self._attachment_node_at(event.position().toPoint()) if copy_allowed else None
            self._set_attachment_drop_node(node_id)
            if node_id is None:
                event.ignore()
            else:
                event.setDropAction(Qt.DropAction.CopyAction)
                event.accept()
            return
        if self.node_drag.update_external(event.mimeData(), event.position().toPoint()):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._attachment_drag_file = None
        self._set_attachment_drop_node(None)
        self.node_drag.cancel_external()
        event.accept()

    def dropEvent(self, event):
        """释放时复查落点后再提交；异常和取消统一清理未提交的影像及滚动。"""
        if event.mimeData().hasUrls():
            path, self._attachment_drag_file = self._attachment_drag_file, None
            self._set_attachment_drop_node(None)
            node_id = self._attachment_node_at(event.position().toPoint())
            if (path is not None and node_id is not None and path == self._local_attachment_path(event)
                    and self.owner.call(lambda: self.owner._start_attachment_import(node_id, path))):
                event.setDropAction(Qt.DropAction.CopyAction)
                event.accept()
            else:
                event.ignore()
            return
        changed = self.owner.call(lambda: self.node_drag.drop_external(
            event.mimeData(), event.position().toPoint()))
        self.node_drag.cancel_external()
        if changed:
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()

    def _local_attachment_path(self, event) -> Path | None:
        if not self.owner.editable or not event.possibleActions() & Qt.DropAction.CopyAction:
            return None
        urls = event.mimeData().urls()
        if len(urls) != 1 or not urls[0].isLocalFile():
            return None
        path = Path(urls[0].toLocalFile())
        return path if path.is_absolute() else None

    def _attachment_node_at(self, position):
        if self.owner._disposed or not self.owner.editable:
            return None
        item = self.itemAt(position)
        if isinstance(item, PageControlItem):
            item = item.node_item
        if isinstance(item, NodeEditor):
            return item.node_id
        return item.node.id if isinstance(item, NodeItem) else self.graph_group_at(position)

    def graph_object_at(self, position, *, layout, stacking, moving=(), roots=(), retain=None, borders=False):
        """按实际叠放顺序命中。拖动时借用起始布局，跳过移动对象并检查后代组循环。"""
        if position is None or stacking is None:
            return None
        point = self.mapToScene(position)
        margin = 4 / self.transform().m11()
        for key in reversed(stacking.objects):
            frame = layout.group_frames.get(key)
            if frame is None:
                if key in moving:
                    continue
                box = layout.nodes[key]
                if (box.x <= point.x() <= box.right and box.y <= point.y() <= box.bottom
                        and self.owner.node_items[key].contains(point - QPointF(box.x, box.y))):
                    return key
            elif key not in roots:
                box = frame.bounds
                pad = margin if borders or retain == key else 0
                rect = QRectF(box.x - pad, box.y - pad, box.width + 2 * pad, box.height + 2 * pad)
                if rect.contains(point):
                    outline = QPainterPath()
                    outline.addRoundedRect(rect, 8 + pad, 8 + pad)
                    if outline.contains(point):
                        return key
        return None

    def graph_group_at(self, position, *, include_body=False):
        """在可见的最前方组上区分标题、边框和内容，边框热区使用固定视口像素。"""
        if not self.owner.graph_group_items:
            return None
        layout = self.owner.layout_result
        key = self.graph_object_at(position, layout=layout, stacking=self.owner._graph_stacking, borders=True)
        frame = layout.group_frames.get(key)
        if frame is not None:
            point = self.mapToScene(position)
            box = frame.bounds
            margin = 4 / self.transform().m11()
            if (include_body or point.y() <= frame.header.bottom or point.y() >= box.bottom - margin
                    or point.x() <= box.x + margin or point.x() >= box.right - margin):
                return key
        return None

    def _graph_cover_paths(self, items, pixel_ratio, *, above=None):
        transform = self.viewportTransform()
        pixel = 1 / (self.transform().m11() * (pixel_ratio or self.viewport().devicePixelRatioF()))
        for item in items:
            rank = item.zValue()
            if above is not None and rank <= above:
                continue
            if isinstance(item, GraphGroupItem):
                shape = item.shape()
            elif isinstance(item, NodeItem):
                shape = item.visual.card_outline(pixel_size=pixel)
            elif isinstance(item, NodeEditor):
                shape = item.shape()
            else:
                continue
            outline = item.deviceTransform(transform).map(shape)
            yield rank, outline.boundingRect(), outline

    def graph_occluders(self, items, *, pixel_ratio=None):
        """一次收集候选图元的遮挡轮廓，供同一选框或同一帧的所有对象复用。"""
        return _GraphOcclusion(self.viewport().rect(), self._graph_cover_paths(items, pixel_ratio))

    def graph_visible_region(self, rank, bounds, *, pixel_ratio=None, occluders=None):
        """返回视口矩形中未被更前方的组框、卡片或编辑器遮住的部分。"""
        if occluders is None:
            scene_rect = self.viewportTransform().inverted()[0].mapRect(bounds)
            items = self.scene().items(scene_rect, Qt.ItemSelectionMode.IntersectsItemBoundingRect)
            candidates = self._graph_cover_paths(items, pixel_ratio, above=rank)
        else:
            candidates = occluders.nearby(bounds)
        visible = QPainterPath()
        visible.addRect(bounds)
        cover = QPainterPath()
        cover.setFillRule(Qt.FillRule.WindingFill)
        for front_rank, rect, outline in candidates:
            if front_rank > rank and rect.intersects(bounds):
                cover.addPath(outline)
        return visible.subtracted(cover) if not cover.isEmpty() else visible

    def graph_point_visible(self, rank, position):
        point = QPointF(position)
        area = QRectF(point.x() - 1, point.y() - 1, 2, 2)
        return self.graph_visible_region(rank, area).contains(point)

    def selected_handle_visible(self, position):
        if self.owner.graph is None:
            return True
        item = self.owner.node_items.get(self.owner.current_node_id())
        return item is not None and self.graph_point_visible(item.zValue(), position)

    def clip_graph_decoration(self, painter, bounds, *, item=None, occluders=None):
        """在已有的画笔保存范围内，按所属节点裁剪局部装饰，保留调用方的绘制坐标。"""
        if self.owner.graph is None:
            return
        if item is None:
            item = self.owner.node_items.get(self.owner.current_node_id())
            if item is None:
                return
        transform = painter.worldTransform()
        painter.resetTransform()
        clip = self.graph_visible_region(item.zValue(), bounds, pixel_ratio=painter.device().devicePixelRatioF(),
                                         occluders=occluders)
        painter.setClipPath(clip, Qt.ClipOperation.IntersectClip)
        painter.setWorldTransform(transform)

    def _set_attachment_drop_node(self, node_id):
        if node_id != self._attachment_drop_node:
            self._attachment_drop_node = node_id
            self.viewport().update()

    def wheelEvent(self, event):
        """滚轮模式仅影响视图；Ctrl 始终临时缩放，不改写用户选择的模式。"""
        self.image_resize.cancel()
        self.text_resize.cancel()
        self.group_resize.cancel()
        pixels = event.pixelDelta().y()
        angle = event.angleDelta().y()
        if not pixels and not angle:
            event.ignore()
            return
        if self.owner.wheel_mode == "zoom" or event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if self.owner.editor is not None:
                self.owner.finish_edit(commit=True)
            self.zoom(1.15 ** ((pixels or angle) / 120), anchor=event.position().toPoint())
        else:
            # 触控板优先使用像素增量；普通滚轮遵循系统滚动行数。保留不足一个像素的
            # 余量，避免高精度滚轮的小步输入被连续取整丢弃；Shift 也不改为横向滚动。
            bar = self.verticalScrollBar()
            self._wheel_scroll_remainder += (
                pixels if pixels else angle / 120 * QApplication.wheelScrollLines() * bar.singleStep()
            )
            distance = int(self._wheel_scroll_remainder)
            self._wheel_scroll_remainder -= distance
            bar.setValue(bar.value() - distance)
        self.update_cursor(event.position().toPoint())
        if self.node_drag.active:
            self.node_drag.update(event.position().toPoint())
        event.accept()

    def zoom(self, factor, *, anchor=None):
        """将缩放比例限制在 10%～400%；指定视口坐标时显式固定鼠标下的场景位置。

        显式锚点不依赖系统光标或上一条鼠标移动事件，因此触控板滚动和合成的 Qt
        滚轮事件也具有同样的定位行为；省略锚点时围绕视口中心缩放。目标位置超出
        内容加一屏余量的范围时，由 Qt 将视口限制到最近边界，不额外扩展空白区域。
        """
        self.image_resize.cancel()
        self.text_resize.cancel()
        self.group_resize.cancel()
        # 切页的初始定位可能还在零间隔队列中。先完成可见页的待处理定位，再接受
        # 用户缩放，避免后续编辑缓存页面状态时把刚设置的倍率恢复成 100%。
        if self.owner._view_reset_pending and self.isVisible():
            self.owner._apply_view_reset()
        current = self.transform().m11()
        target = min(4.0, max(MIN_ZOOM, current * factor))
        anchor = self.viewport().rect().center() if anchor is None else anchor
        before = self.mapToScene(anchor)
        previous_anchor = self.transformationAnchor()
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        try:
            with self._batch_viewport_changes():
                self.scale(target / current, target / current)
                # 先按缩放后的坐标映射反推目标中心，再一次性更新范围和中心。用滚动
                # 位置补偿锚点，而非把位移累加到变换矩阵，保证边界始终约束实际视口。
                visible = self.mapToScene(self.viewport().rect()).boundingRect()
                center = visible.center() + before - self.mapToScene(anchor)
                self.update_navigation_rect(center=center)
        finally:
            self.setTransformationAnchor(previous_anchor)
        self.update_cursor()
        if self.node_drag.active:
            self.node_drag.update(self.node_drag._pointer)

    @contextmanager
    def _batch_viewport_changes(self):
        """Qt 分别滚动横纵轴时只同步最终视口，避免中间位置反复创建、回收图元。"""
        self._viewport_sync_depth += 1
        try:
            yield
        finally:
            self._viewport_sync_depth -= 1
            if not self._viewport_sync_depth:
                self.owner._viewport_changed()

    def update_navigation_rect(self, *, center: QPointF | None = None, keep_visible=False) -> None:
        """按当前可见内容重算导航范围，四周各留一屏可平移余量。

        场景范围由组件提供，只包含布局结果；视图范围另加按当前倍率换算的一屏
        宽度和高度，不累计历史边界或当前位置，因此内容减少后也能收缩。center
        为希望保留的场景中心，省略时保留当前中心。keep_visible 让本次节点重排也
        容纳原视口，其他操作越界时由滚动条钳制。
        此方法仅调整视图，不修改文档、布局、缩放比例或导出的内容范围。
        """
        if self.scene() is None or self.viewport().size().isEmpty():
            return
        visible = self.mapToScene(self.viewport().rect()).boundingRect()
        width, height = visible.width(), visible.height()
        center = visible.center() if center is None else center
        bounds = self.scene().sceneRect().adjusted(-width, -height, width, height)
        if keep_visible:
            visible.moveCenter(center)
            bounds = bounds.united(visible)
        with self._batch_viewport_changes():
            self.setSceneRect(bounds)
            self.centerOn(center)

    def scrollContentsBy(self, dx, dy):
        """原生滚动完成后按新逻辑视口同步图元，边缘自动滚动也复用此入口。"""
        super().scrollContentsBy(dx, dy)
        self.owner._viewport_changed()
        if self.graph_edge_edit.active:
            # 滚动条可以在鼠标未移动时改变落点，连线预览须从当前视口重新绘制。
            self.viewport().update()

    def showEvent(self, event):
        """首次显示或隐藏标签重新可见时，唤醒画布尚未完成的初始定位请求。"""
        super().showEvent(event)
        # Qt 重新显示视图时会重算滚动范围；显式恢复隐藏前的中心，避免标签或
        # 窗口隐藏期间的布局变化把用户位置换成新视口的左上角附近。
        if self._hidden_center is not None:
            self.update_navigation_rect(center=self._hidden_center)
            self._hidden_center = None
        self.owner._schedule_view_reset()

    def hideEvent(self, event):
        """在 Qt 重建隐藏视图的布局前记住实际中心，供下一次显示或延迟调整尺寸使用。"""
        self.cancel_interaction()
        self._hidden_center = self.mapToScene(self.viewport().rect()).boundingRect().center()
        super().hideEvent(event)

    def resizeEvent(self, event):
        # Qt 派发该事件时视口已具有新尺寸。用事件中的旧尺寸恢复原中心，避免窗口
        # 放大或缩小时把旧左上角当作锚点；首次显示没有旧尺寸，交给初始化居中处理。
        center = self._hidden_center
        if center is None and event.oldSize().isValid():
            old_rect = QRect(0, 0, event.oldSize().width(), event.oldSize().height())
            center = self.mapToScene(old_rect).boundingRect().center()
        super().resizeEvent(event)
        self.update_navigation_rect(center=center)
        # 仅待初始化的画布会排程；正常窗口及侧栏调整继续保持原有中心和倍率。
        self.owner._schedule_view_reset()

    def _schedule_fold_hide(self) -> None:
        """用一个视图定时器调度临近的隐藏请求，节点无需创建额外的 Qt 定时对象。"""
        if not self._fold_hide_requests:
            self._fold_hide_timer.stop()
            return
        deadline = min(value[1] for value in self._fold_hide_requests.values())
        self._fold_hide_timer.start(max(1, ceil((deadline - monotonic()) * 1000)))

    def _defer_fold_hide(self, item: NodeItem) -> None:
        """为刚离开的节点登记 200 毫秒期限，弱引用不阻止屏外图元或旧画布释放。"""
        item._fold_hide_pending = True
        self._fold_hide_requests[item.node.id] = ref(item), monotonic() + FOLD_HIDE_DELAY / 1000
        self._schedule_fold_hide()

    def _cancel_fold_hide(self, item: NodeItem) -> None:
        """重入或状态变化时撤回该图元的请求，避免同标识的新旧图元互相影响。"""
        pending = self._fold_hide_requests.get(item.node.id)
        if pending is not None and pending[0]() is item:
            del self._fold_hide_requests[item.node.id]
            self._schedule_fold_hide()
        item._fold_hide_pending = False

    def _expire_fold_hovers(self, *, clear=False) -> None:
        """只刷新到期且仍存活的按钮；取消手势时清空请求，不访问已销毁的 Qt 对象。"""
        now = monotonic()
        for key, (reference, deadline) in list(self._fold_hide_requests.items()):
            if clear or deadline <= now:
                del self._fold_hide_requests[key]
                item = reference()
                if item is not None and isValid(item):
                    item._fold_hide_pending = False
                    item.fold_control.sync_visibility()
        self._schedule_fold_hide()

    def _fold_hover_at(self, position) -> NodeItem | None:
        """借用 Qt 空间索引查找出线附近的节点，即使按钮隐藏也可唤出。

        查询范围仅覆盖父子间距的前半段和 10 像素余量，不枚举 node_items 或完整
        布局结果。候选再按各自方向精确检测；多个热区接近时选择距离最近者，避免
        一次唤出多个层级。正文及已显示按钮在调用方优先命中，保留原有编辑与拖动。
        """
        scale = self.transform().m11()
        radius = ceil(32 * scale + FOLD_HOVER_PADDING + 2)
        area = QRect(position.x() - radius, position.y() - radius, radius * 2 + 1, radius * 2 + 1)
        scene_position = self.mapToScene(position)
        nearest, score = None, None
        for item in self.items(area, Qt.ItemSelectionMode.IntersectsItemBoundingRect):
            if not isinstance(item, NodeItem) or item.fold_control is None:
                continue
            control = item.fold_control
            distance = control.hover_distance(scene_position, scale)
            if distance is None:
                continue
            offset = control.mapToScene(control.button_rect().center()) - scene_position
            candidate = distance, offset.x() ** 2 + offset.y() ** 2
            if score is None or candidate < score:
                nearest, score = item, candidate
        return nearest

    def _set_hovered_node(self, item: NodeItem | None, *, immediate: bool = False) -> None:
        """进入新热区立即显示，移出旧热区延迟隐藏；只更新涉及的节点按钮。"""
        if item is not self._hovered_node:
            previous, self._hovered_node = self._hovered_node, item
            if previous is not None and isValid(previous):
                previous.set_hovered(False, immediate=immediate)
            if item is not None:
                item.set_hovered(True)

    def update_cursor(self, position=None):
        """按手势优先级更新视口光标，防止图元的悬停光标覆盖正在进行的平移。

        缓存最近一次视口坐标，供只读切换、编辑结束和场景刷新后立即重新命中；
        根节点不能移动，折叠按钮及只读节点也不会显示可拖动的手形。
        """
        if position is not None:
            self._pointer_position = position
        position = self._pointer_position
        resize_cursor = self.image_resize.cursor_at(position)
        if (self.text_resize.active or self._pan_button == Qt.MouseButton.NoButton
                and self.press_node is None and not self.graph_edge_edit.owns_left_button
                and self.text_resize.hit_test(position)):
            resize_cursor = Qt.CursorShape.SizeHorCursor
        if (self.group_resize.active or self._pan_button == Qt.MouseButton.NoButton
                and self.press_node is None and not self.graph_edge_edit.owns_left_button
                and self.group_resize.hit_test(position)):
            resize_cursor = Qt.CursorShape.SizeFDiagCursor
        dragging = self.node_drag.active
        if resize_cursor is not None and not self.panning and not dragging:
            self._set_hovered_node(None, immediate=True)
            self.viewport().setCursor(resize_cursor)
            return
        hit = None
        inside = position is not None and self.viewport().rect().contains(position)
        if inside:
            hit = self.itemAt(position)
        edge_cursor = self.graph_edge_edit.update_hover(
            position if inside else None, hit,
            blocked=self._pan_button != Qt.MouseButton.NoButton or self.press_node is not None or dragging)
        if edge_cursor is not None:
            self._set_hovered_node(None, immediate=True)
            self.viewport().setCursor(edge_cursor)
            return
        hovered = hit.node_item if isinstance(hit, (FoldControlItem, PageControlItem)) else hit
        if inside:
            if not isinstance(hovered, (NodeItem, NodeEditor)) and not self.panning and not dragging:
                hovered = self._fold_hover_at(position)
        self._set_hovered_node(
            hovered if isinstance(hovered, NodeItem) and not self.panning and not dragging else None,
            immediate=self.panning or dragging,
        )
        # 冷启动悬停可能刚刚唤出指针下的按钮，同一帧即使用按钮光标。
        if isinstance(hovered, NodeItem) and hovered.fold_control is not None and position is not None:
            if hovered.fold_control.contains_button(self.mapToScene(position)):
                hit = hovered.fold_control
        if self.panning or dragging:
            shape = Qt.CursorShape.ForbiddenCursor if self.node_drag.graph_drop_blocked else Qt.CursorShape.ClosedHandCursor
        elif self._pan_button != Qt.MouseButton.NoButton:
            shape = Qt.CursorShape.OpenHandCursor
        elif position is None or not self.viewport().rect().contains(position):
            self.viewport().unsetCursor()
            return
        else:
            item = self.scene().mouseGrabberItem() or hit
            shape = Qt.CursorShape.ArrowCursor
            if isinstance(item, NodeEditor):
                shape = Qt.CursorShape.IBeamCursor
            elif isinstance(item, PageControlItem):
                if item.can_activate():
                    shape = Qt.CursorShape.PointingHandCursor
            elif isinstance(item, FoldControlItem):
                if item.can_activate() and item.contains_button(self.mapToScene(position)):
                    shape = Qt.CursorShape.PointingHandCursor
            elif isinstance(item, NodeItem) and self.owner.editable and self.owner.editor is None:
                if (self.owner.current_page_kind in ("inspiration", "graph")
                        or item.node.id not in (self.owner.document_view.root_id, self.owner.focus_id)):
                    shape = Qt.CursorShape.OpenHandCursor
            elif self.owner.editable and self.graph_group_at(position) is not None:
                shape = Qt.CursorShape.OpenHandCursor
        self.viewport().setCursor(shape)

    def cancel_interaction(self):
        """捕获中断时丢弃临时手势，不提交节点移动，也不补发右键菜单。"""
        self.panning = False
        self._pan_button = Qt.MouseButton.NoButton
        self._pan_origin = None
        self.press_node = None
        self._fold_pressed = False
        self._selection_click = None
        self._attachment_drag_file = None
        self._set_attachment_drop_node(None)
        self._set_hovered_node(None, immediate=True)
        self._expire_fold_hovers(clear=True)
        self.node_drag.cancel()
        self.graph_edge_edit.cancel()
        self.image_resize.cancel()
        self.text_resize.cancel()
        self.group_resize.cancel()
        grabber = self.scene().mouseGrabberItem()
        if isinstance(grabber, NodeItem):
            grabber.ungrabMouse()
        self.update_cursor()

    def focusOutEvent(self, event):
        self.cancel_interaction()
        super().focusOutEvent(event)

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.UngrabMouse:
            self.cancel_interaction()
        result = super().viewportEvent(event)
        if event.type() == QEvent.Type.Leave and self._pan_button == Qt.MouseButton.NoButton:
            self.node_drag.leave()
            self._pointer_position = None
            self.update_cursor()
        return result

    def mousePressEvent(self, event):
        if self._active_resize is not None:
            event.accept()
            return
        # 一个手势只由最初按下的按键拥有；额外按下或释放其他按键都不能切换手势。
        if self._pan_button != Qt.MouseButton.NoButton or event.buttons() != event.button():
            event.accept()
            return
        if self.graph_edge_edit.owns_left_button:
            # 失去捕获后可能收不到上一次释放，新的独立按下可直接接管视口。
            self.graph_edge_edit.owns_left_button = False
            self.graph_edge_edit.cancel()
        # 新操作先完成上次放下后的展示动画，随后按最终几何命中，避免拖到中间帧。
        self.node_drag._settle_reflow()
        self.press_node = None
        self._selection_click = None
        if event.button() in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton) or (
            event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() & Qt.KeyboardModifier.AltModifier
        ):
            self._pan_button = event.button()
            self._pan_origin = event.position().toPoint()
            self.panning = event.button() != Qt.MouseButton.RightButton
            self.last_pan = event.position().toPoint()
            self.update_cursor(event.position().toPoint())
            event.accept()
            return
        position = event.position().toPoint()
        if self.owner.is_editing and event.button() == Qt.MouseButton.LeftButton:
            hit = self.itemAt(position)
            if isinstance(hit, GraphGroupItem):
                self.owner.finish_edit(commit=True)
                if self.owner.is_editing or self.itemAt(position) is not hit:
                    event.accept()
                    return
        if (event.button() == Qt.MouseButton.LeftButton and not event.modifiers()
                and self.group_resize.hit_test(position)):
            self.owner.call(lambda: self.group_resize.begin(position))
            self.update_cursor(position)
            event.accept()
            return
        if (event.button() == Qt.MouseButton.LeftButton and not event.modifiers()
                and self.text_resize.hit_test(position)):
            self.owner.call(lambda: self.text_resize.begin(position))
            self.update_cursor(position)
            event.accept()
            return
        if (event.button() == Qt.MouseButton.LeftButton
                and self.graph_edge_edit.press_left(position, event.modifiers())):
            # 前景手柄和连线不属于场景图元。本次按下由视图接管，不能再交给框选或节点拖动。
            self.update_cursor(position)
            event.accept()
            return
        item = self.itemAt(position)
        handle = self.image_resize.hit_test(position)
        if (handle is not None and event.button() == Qt.MouseButton.LeftButton
                and not isinstance(item, PageControlItem)):
            self.owner.call(lambda: self.image_resize.begin(handle, event.position().toPoint()))
            self.update_cursor(event.position().toPoint())
            event.accept()
            return
        if isinstance(item, PageControlItem) and event.button() == Qt.MouseButton.LeftButton:
            if item.can_activate():
                # 切页会释放当前图元，回调只捕获节点 ID，不能在被销毁的按钮实例上继续执行。
                key = item.node_item.node.id
                enter = {"extension": self.owner.extension_page_requested.emit,
                         "graph": self.owner.open_node_graph,
                         "attachment": self.owner.open_node_attachment,
                         "detail": self.owner.enter_detail_page, "submap": self.owner.enter_submap,
                         "reference": self.owner.open_page_reference}[item.kind]
                self.owner.call(lambda: enter(key))
            self._fold_pressed = True
            event.accept()
            return
        if isinstance(item, FoldControlItem) and event.button() == Qt.MouseButton.LeftButton:
            if item.can_activate() and item.contains_button(self.mapToScene(event.position().toPoint())):
                node = item.node_item.node
                self.owner.call(lambda: self.owner.engine.set_expanded(node.id, not node.expanded))
            # 引擎同步会调用 cancel_interaction，必须在提交返回后认领本次按钮手势。
            # 后续移动和释放均不交给场景，避免产生框选、正文拖动或编辑器失焦提交。
            self._fold_pressed = True
            event.accept()
            self.update_cursor(event.position().toPoint())
            return
        if isinstance(item, NodeItem) and event.button() == Qt.MouseButton.LeftButton:
            # 多选节点中的一个被拖动时保持整组选择，普通点击仍采用 Qt 的选择规则。
            preserve = not event.modifiers() or (self.owner.graph is not None
                                                  and event.modifiers() == Qt.KeyboardModifier.ShiftModifier)
            multiple = self.owner.selection_count > 1 or self.owner.graph_edge_selection_count
            if item.isSelected() and multiple and preserve:
                self._selection_click = item.node.id
                event.accept()
            elif self.owner.graph is not None and event.modifiers() == Qt.KeyboardModifier.ShiftModifier:
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                self.owner.select_nodes([item.node.id])
                event.accept()
            else:
                with self.owner._scene_selection_event(
                    replace=not bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                ):
                    super().mousePressEvent(event)
            if (
                self.owner.editable
                and self.owner.editor is None
                and (self.owner.current_page_kind in ("inspiration", "graph")
                     or item.node.id not in (self.owner.document_view.root_id, self.owner.focus_id))
            ):
                self.press_node = item.node.id
                self.press_position = event.position().toPoint()
                self.drag_origin = self.mapToScene(self.press_position)
            self.update_cursor(event.position().toPoint())
            return
        group = self.graph_group_at(position)
        if group is not None and event.button() == Qt.MouseButton.LeftButton and not isinstance(item, NodeEditor):
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.owner._set_canvas_selection(
                    self.owner._selected_ids ^ {group}, self.graph_edge_edit.selected_edges)
            elif not self.owner.is_node_selected(group):
                self.owner.select_nodes([group])
            elif self.owner.selection_count > 1 or self.owner.graph_edge_selection_count:
                self._selection_click = group
            if self.owner.is_node_selected(group) and self.owner.editable and not self.owner.is_editing:
                self.press_node = group
                self.press_position = position
                self.drag_origin = self.mapToScene(position)
            self.update_cursor(position)
            event.accept()
            return
        with self.owner._scene_selection_event(
            replace=not isinstance(item, NodeEditor)
            and not bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        ):
            super().mousePressEvent(event)
        self.update_cursor(event.position().toPoint())

    def mouseMoveEvent(self, event):
        if self.graph_edge_edit.owns_left_button:
            position = event.position().toPoint()
            if event.buttons() & Qt.MouseButton.LeftButton:
                self.graph_edge_edit.move(position)
                self.update_cursor(position)
                event.accept()
                return
            self.graph_edge_edit.owns_left_button = False
            self.graph_edge_edit.cancel()
        resize = self._active_resize
        if resize is not None:
            if event.buttons() & Qt.MouseButton.LeftButton:
                resize.update(event.position().toPoint())
            else:
                resize.cancel()
            self.update_cursor(event.position().toPoint())
            event.accept()
            return
        if self._fold_pressed:
            if not event.buttons() & Qt.MouseButton.LeftButton:
                self._fold_pressed = False
            self.update_cursor(event.position().toPoint())
            event.accept()
            return
        if self._pan_button != Qt.MouseButton.NoButton:
            if not event.buttons() & self._pan_button:
                self.cancel_interaction()
                event.accept()
                return
            position = event.position().toPoint()
            if not self.panning:
                if (position - self._pan_origin).manhattanLength() < QApplication.startDragDistance():
                    self.update_cursor(position)
                    event.accept()
                    return
                # 右键先等待系统拖动距离；越过阈值后从按下位置开始累计完整位移。
                self.panning = True
            delta = position - self.last_pan
            with self._batch_viewport_changes():
                self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
                self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            self.last_pan = position
            self.update_cursor(position)
            event.accept()
            return
        pressed = self.press_node is not None
        if pressed and event.buttons() & Qt.MouseButton.LeftButton:
            if (
                self.owner.editable and not self.node_drag.active
                and (event.position().toPoint() - self.press_position).manhattanLength()
                >= QApplication.startDragDistance()
            ):
                self._selection_click = None
                if not self.node_drag.begin(self.drag_origin):
                    self.press_node = None
                    event.accept()
                    return
            if self.node_drag.active:
                if self.owner.graph is None and not self.viewport().rect().contains(event.position().toPoint()):
                    roots = tuple(self.node_drag._resolver.roots)
                    self.owner.call(lambda: self.owner.start_external_branch_drag(roots))
                    event.accept()
                    return
                self.node_drag.update(event.position().toPoint(), event.modifiers())
                self.update_cursor(event.position().toPoint())
                event.accept()
                return
        elif pressed:
            # 捕获丢失后仍可能收到没有左键状态的移动，不能留下悬挂预览和计时器。
            self.cancel_interaction()
        with self.owner._scene_selection_event():
            super().mouseMoveEvent(event)
        self.update_cursor(event.position().toPoint())

    def mouseReleaseEvent(self, event):
        if self.graph_edge_edit.owns_left_button:
            if event.button() == Qt.MouseButton.LeftButton:
                position = event.position().toPoint()
                self.owner.call(lambda: self.graph_edge_edit.release(position))
                self.update_cursor(position)
            event.accept()
            return
        resize = self._active_resize
        if resize is not None:
            if event.button() == Qt.MouseButton.LeftButton:
                self.owner.call(lambda: resize.finish(event.position().toPoint()))
            self.update_cursor(event.position().toPoint())
            event.accept()
            return
        if self._fold_pressed and event.button() == Qt.MouseButton.LeftButton:
            self._fold_pressed = False
            self.update_cursor(event.position().toPoint())
            event.accept()
            return
        if self._pan_button != Qt.MouseButton.NoButton:
            if event.button() != self._pan_button:
                event.accept()
                return
            show_menu = self._pan_button == Qt.MouseButton.RightButton and not self.panning
            self.cancel_interaction()
            self.update_cursor(event.position().toPoint())
            event.accept()
            if show_menu:
                self._show_context_menu(event.position().toPoint(), event.globalPosition().toPoint())
            return
        if event.button() != Qt.MouseButton.LeftButton:
            event.accept()
            return
        if self.node_drag.active:
            self._selection_click = None
            drop = self.node_drag.finish(event.position().toPoint(), event.modifiers())
            self.press_node = None
            # 节点按下可能由场景接管，配对释放解除图元捕获后才能提交。
            with self.owner._scene_selection_event():
                super().mouseReleaseEvent(event)
            if drop is not None:
                self.owner.call(lambda: self.node_drag.commit(drop))
            self.update_cursor(event.position().toPoint())
            event.accept()
            return
        self.press_node = None
        with self.owner._scene_selection_event():
            super().mouseReleaseEvent(event)
        if self._selection_click is not None:
            key, self._selection_click = self._selection_click, None
            self.owner.select_nodes([key])
        self.update_cursor(event.position().toPoint())

    def mouseDoubleClickEvent(self, event):
        """按钮双击仍按独立点击处理，不把事件交给正文的双击编辑入口。"""
        position = event.position().toPoint()
        if event.button() == Qt.MouseButton.LeftButton and (
                self.graph_edge_edit.handle_at(position) or not event.modifiers() and (
                    self.text_resize.hit_test(position) or self.group_resize.hit_test(position))):
            self.mousePressEvent(event)
            return
        if event.button() == Qt.MouseButton.LeftButton and isinstance(self.itemAt(position), GraphGroupItem):
            if not event.modifiers() and self.owner.editable and self.graph_group_at(position) is None:
                group = self.graph_group_at(position, include_body=True)
                if group is not None and self.owner.graph_edge_at(position) is None:
                    point = self.mapToScene(position)
                    self.owner.call(lambda: self.owner.add_graph_group_member(group, (point.x(), point.y())))
            event.accept()
            return
        if (event.button() == Qt.MouseButton.LeftButton and self.owner.current_page_kind == "inspiration"
                and self.itemAt(event.position().toPoint()) is None and self.owner.editable):
            self.owner.call(self.owner.add_inspiration)
            event.accept()
            return
        if self._fold_pressed or isinstance(self.itemAt(event.position().toPoint()),
                                           (FoldControlItem, PageControlItem)):
            self.mousePressEvent(event)
        else:
            super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        """鼠标菜单统一由右键释放决定，避免不同平台在按下/释放时重复触发。

        拖动结束后系统仍可能派发鼠标菜单事件，因此不能在释放时清空一个抑制标记
        就交还默认处理。键盘菜单事件独立保留，不受上一轮右键拖动影响。
        """
        event.accept()
        if event.reason() != event.Reason.Mouse:
            if self.graph_edge_edit.selected_edges:
                self.owner.call(lambda: self.owner._show_graph_edge_menu(event.globalPos()))
            else:
                self.owner.context_menu(event.globalPos())

    def _show_context_menu(self, position, global_position):
        """仅在确定为点击后选择菜单目标，右键拖动始终保留原有选择。"""
        handle = self.graph_edge_edit.handle_at(position)
        if handle is not None and handle.original is not None:
            self.owner.call(lambda: self.owner._show_graph_edge_menu(global_position))
            return
        item = self.itemAt(position)
        if isinstance(item, (FoldControlItem, PageControlItem)):
            item = item.node_item
        if isinstance(item, NodeItem) and not item.group_title:
            if not item.isSelected():
                self.owner.select_nodes([item.node.id])
            if self.owner.graph is not None:
                self.owner.context_menu(global_position, node_id=item.node.id)
            else:
                self.owner.context_menu(global_position)
            return
        group = item.node.id if isinstance(item, NodeItem) else self.graph_group_at(position)
        if group is None and self.owner.call(lambda: self.owner.graph_edge_context_menu(position, global_position)):
            return
        group = group or self.graph_group_at(position, include_body=True)
        if group is not None:
            self.owner.context_menu(global_position, group_id=group)
        else:
            self.graph_edge_edit.clear_selection()
            self.owner.context_menu(global_position, on_canvas=True)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Shift and self.node_drag.set_keep_graph_layer(True):
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and (
                self.press_node is not None or self._active_resize is not None
                or self.node_drag.external_active or self.graph_edge_edit.active):
            self.cancel_interaction()
            event.accept()
            return
        directions = {
            Qt.Key.Key_Left: (-1, 0),
            Qt.Key.Key_Right: (1, 0),
            Qt.Key.Key_Up: (0, -1),
            Qt.Key.Key_Down: (0, 1),
        }
        if self.owner.editor is None and event.key() in directions and not event.modifiers():
            if self.owner.selection_count:
                key = self.owner.current_node_id()
                self.owner._ensure_node_item(key)
                current = self.owner.node_items[key].sceneBoundingRect().center()
                dx, dy = directions[event.key()]
                if self.owner._virtualized:
                    target = self.owner.layout_result.geometry.nearest(key, dx, dy)
                    if target is not None:
                        self.owner.select_nodes([target])
                        self.ensureVisible(self.owner.node_items[target], 50, 50)
                    event.accept()
                    return
                candidates = []
                for node_id, item in self.owner.node_items.items():
                    offset = item.sceneBoundingRect().center() - current
                    along = offset.x() * dx + offset.y() * dy
                    across = abs(offset.y() if dx else offset.x())
                    if along > 1:
                        candidates.append((along + across * 2, node_id))
                if candidates:
                    target = min(candidates)[1]
                    self.owner.select_nodes([target])
                    self.ensureVisible(self.owner.node_items[target], 50, 50)
            event.accept()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_Shift and self.node_drag.set_keep_graph_layer(False):
            event.accept()
            return
        super().keyReleaseEvent(event)

    def _update_selection_decoration(self, *_args):
        """使新旧外框范围失效，覆盖文字伸缩、节点移动和移除，不触碰场景几何。

        由 Qt 的场景变化信号在 GUI 线程调用，参数中的脏场景矩形不需保留；释放
        后忽略迟到通知。viewport.update 只请求重绘，不再次触发场景变化信号。
        """
        if self.owner._disposed:
            return
        self._pending_selection_bounds = self._pending_selection_bounds.united(self._selection_bounds)
        self._selection_decoration.invalidate()
        if not self._selection_update_timer.isActive():
            self._selection_update_timer.start(0)

    def _flush_selection_decoration(self):
        """同一事件循环只计算一次新旧外框范围，供随后前景绘制复用。"""
        if self.owner._disposed:
            return
        current = selection_bounds(self).united(self.text_resize.decoration_bounds())
        current = current.united(self.group_resize.decoration_bounds())
        dirty = self._pending_selection_bounds.united(self._selection_bounds).united(current)
        self._pending_selection_bounds = QRect()
        self._selection_bounds = current
        if not dirty.isEmpty():
            self.viewport().update(dirty)

    def drawForeground(self, painter, rect):
        """在 GUI 视图前景绘制间隔选中外框、调宽预览与尺寸手柄，不参与场景导出。

        painter、rect 为 Qt 本次绘制借用对象，不缓存、不修改文档或资源；仅由
        Qt GUI 线程在存活视图上调用，无额外所有权或正常业务异常。
        """
        super().drawForeground(painter, rect)
        self._draw_inspiration_empty(painter)
        if self.owner.graph is not None and not self.owner.document_view.nodes and self.owner.editable:
            painter.save()
            painter.resetTransform()
            painter.setPen(QColor(self.owner.ui_colors.muted))
            painter.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter,
                             "按 Tab 添加节点，或右键选择“编辑图文本”")
            painter.restore()
        occluders = None
        if self.owner.graph is not None and self.owner.selection_count + self.owner.graph_edge_selection_count > 1:
            items = self.items(self.viewport().rect(), Qt.ItemSelectionMode.IntersectsItemBoundingRect)
            occluders = self.graph_occluders(items, pixel_ratio=painter.device().devicePixelRatioF())
        paint_node_selection(self, painter, occluders=occluders)
        target = self.owner.node_items.get(self._attachment_drop_node)
        if target is not None:
            painter.save()
            accent = QColor(self.owner.ui_colors.accent)
            pen = QPen(accent, 2)
            pen.setCosmetic(True)
            painter.setPen(pen)
            accent.setAlpha(36)
            painter.setBrush(accent)
            outline = target.visual.card_outline(pixel_size=1 / self.transform().m11())
            bounds = target.deviceTransform(self.viewportTransform()).mapRect(outline.boundingRect())
            self.clip_graph_decoration(painter, bounds.adjusted(-2, -2, 2, 2), item=target)
            painter.drawPath(target.mapToScene(outline))
            painter.restore()
        # 缩放及滚动可能只重绘视图而不发出 scene.changed；记住实际显示的位置，
        # 使随后的局部移动仍能完整擦除旧外框，而非使用缩放前的视口坐标。
        self._selection_bounds = self._selection_decoration.bounds.united(self.text_resize.decoration_bounds())
        self._selection_bounds = self._selection_bounds.united(self.group_resize.decoration_bounds())
        self.image_resize.paint(painter)
        self.graph_edge_edit.paint(painter, occluders=occluders)
        self.text_resize.paint(painter)
        self.group_resize.paint(painter)
        self.node_drag.paint_graph_hint(painter)
