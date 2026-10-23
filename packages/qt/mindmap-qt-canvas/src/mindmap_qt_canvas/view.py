"""画布事件路由：平移、缩放、节点拖拽和键盘空间导航。"""

from math import ceil
from time import monotonic
from weakref import ref

from PySide6.QtCore import QEvent, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QApplication, QGraphicsView
from shiboken6 import isValid

from .dragging import NodeDragController
from .items import (
    FOLD_HIDE_DELAY,
    FOLD_HOVER_PADDING,
    FoldControlItem,
    NodeEditor,
    NodeItem,
    SubmapControlItem,
)

# 交互缩放与“适应窗口”共用 10% 下限，避免自动适配绕过交互入口的倍率限制。
MIN_ZOOM = 0.1


class MindMapView(QGraphicsView):
    def __init__(self, owner, scene):
        super().__init__(scene, owner)
        self.owner = owner
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
        self.drag_origin = QPointF()
        self.node_drag = NodeDragController(self)
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

    @property
    def drag_ghost(self):
        """保留视图内部的预览查询入口，图元的创建和释放统一归拖动控制器管理。"""
        return self.node_drag.ghost

    def event(self, event):
        # QAction 会在 keyPressEvent 前解析 Escape；必须先认领快捷键，否则现有
        # “返回完整导图”动作会改变聚焦范围，无法只取消当前手势。
        if (
            event.type() == QEvent.Type.ShortcutOverride
            and event.key() == Qt.Key.Key_Escape
            and self.press_node is not None
        ):
            event.accept()
            return True
        return super().event(event)

    def wheelEvent(self, event):
        """滚轮模式仅影响视图；Ctrl 始终临时缩放，不改写用户选择的模式。"""
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
        current = self.transform().m11()
        target = min(4.0, max(MIN_ZOOM, current * factor))
        anchor = self.viewport().rect().center() if anchor is None else anchor
        before = self.mapToScene(anchor)
        previous_anchor = self.transformationAnchor()
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        try:
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

    def update_navigation_rect(self, *, center: QPointF | None = None) -> None:
        """按当前可见内容重算导航范围，四周各留一屏可平移余量。

        场景范围由组件提供，只包含布局结果；视图范围另加按当前倍率换算的一屏
        宽度和高度，不累计历史边界或当前位置，因此内容减少后也能收缩。center
        为希望保留的场景中心，省略时保留当前中心；越界由内部滚动条自动钳制。
        此方法仅调整视图，不修改文档、布局、缩放比例或导出的内容范围。
        """
        if self.scene() is None or self.viewport().size().isEmpty():
            return
        visible = self.mapToScene(self.viewport().rect()).boundingRect()
        width, height = visible.width(), visible.height()
        center = visible.center() if center is None else center
        self.setSceneRect(self.scene().sceneRect().adjusted(-width, -height, width, height))
        self.centerOn(center)
        self.owner._viewport_changed()

    def scrollContentsBy(self, dx, dy):
        """原生滚动完成后按新逻辑视口同步图元，边缘自动滚动也复用此入口。"""
        super().scrollContentsBy(dx, dy)
        self.owner._viewport_changed()

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
        hit = None
        inside = position is not None and self.viewport().rect().contains(position)
        if inside:
            hit = self.itemAt(position)
        hovered = hit.node_item if isinstance(hit, (FoldControlItem, SubmapControlItem)) else hit
        if inside:
            if not isinstance(hovered, (NodeItem, NodeEditor)) and not self.panning and self.drag_ghost is None:
                hovered = self._fold_hover_at(position)
        self._set_hovered_node(
            hovered if isinstance(hovered, NodeItem) and not self.panning and self.drag_ghost is None else None,
            immediate=self.panning or self.drag_ghost is not None,
        )
        # 冷启动悬停可能刚刚唤出指针下的按钮，同一帧即使用按钮光标。
        if isinstance(hovered, NodeItem) and hovered.fold_control is not None and position is not None:
            if hovered.fold_control.contains_button(self.mapToScene(position)):
                hit = hovered.fold_control
        if self.panning or self.drag_ghost is not None:
            shape = Qt.CursorShape.ClosedHandCursor
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
            elif isinstance(item, SubmapControlItem):
                if item.can_activate():
                    shape = Qt.CursorShape.PointingHandCursor
            elif isinstance(item, FoldControlItem):
                if item.can_activate() and item.contains_button(self.mapToScene(position)):
                    shape = Qt.CursorShape.PointingHandCursor
            elif isinstance(item, NodeItem) and self.owner.editable and self.owner.editor is None:
                if item.node.id not in (self.owner.document_view.root_id, self.owner.focus_id):
                    shape = Qt.CursorShape.OpenHandCursor
        self.viewport().setCursor(shape)

    def cancel_interaction(self):
        """捕获中断时丢弃临时手势，不提交节点移动，也不补发右键菜单。"""
        self.panning = False
        self._pan_button = Qt.MouseButton.NoButton
        self._pan_origin = None
        self.press_node = None
        self._fold_pressed = False
        self._set_hovered_node(None, immediate=True)
        self._expire_fold_hovers(clear=True)
        self.node_drag.cancel()
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
        # 一个手势只由最初按下的按键拥有；额外按下或释放其他按键都不能切换手势。
        if self._pan_button != Qt.MouseButton.NoButton or event.buttons() != event.button():
            event.accept()
            return
        # 新操作先完成上次放下后的展示动画，随后按最终几何命中，避免拖到中间帧。
        self.node_drag._settle_reflow()
        self.press_node = None
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
        item = self.itemAt(event.position().toPoint())
        if isinstance(item, SubmapControlItem) and event.button() == Qt.MouseButton.LeftButton:
            if item.can_activate():
                key = item.node_item.node.id
                self.owner.call(lambda: self.owner.enter_submap(key))
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
            if item.isSelected() and len(self.owner.selected_node_ids()) > 1 and not event.modifiers():
                event.accept()
            else:
                super().mousePressEvent(event)
            if (
                self.owner.editable
                and self.owner.editor is None
                and item.node.id not in (self.owner.document_view.root_id, self.owner.focus_id)
            ):
                self.press_node = item.node.id
                self.press_position = event.position().toPoint()
                self.drag_origin = self.mapToScene(self.press_position)
            self.update_cursor(event.position().toPoint())
            return
        super().mousePressEvent(event)
        self.update_cursor(event.position().toPoint())

    def mouseMoveEvent(self, event):
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
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            self.last_pan = position
            self.update_cursor(position)
            event.accept()
            return
        if self.press_node and event.buttons() & Qt.MouseButton.LeftButton:
            if (
                self.drag_ghost is None
                and (event.position().toPoint() - self.press_position).manhattanLength()
                >= QApplication.startDragDistance()
            ):
                if not self.node_drag.begin(self.drag_origin):
                    self.press_node = None
                    event.accept()
                    return
            if self.drag_ghost is not None:
                self.node_drag.update(event.position().toPoint())
                self.update_cursor(event.position().toPoint())
                event.accept()
                return
        elif self.press_node:
            # 捕获丢失后仍可能收到没有左键状态的移动，不能留下悬挂预览和计时器。
            self.cancel_interaction()
        super().mouseMoveEvent(event)
        self.update_cursor(event.position().toPoint())

    def mouseReleaseEvent(self, event):
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
        if self.drag_ghost is not None:
            drop = self.node_drag.finish(event.position().toPoint())
            self.press_node = None
            # 节点预览由视图自己处理，但按下事件曾交给场景选择。必须让场景收到
            # 配对的释放事件，解除图元捕获，随后光标才能按释放位置重新命中。
            super().mouseReleaseEvent(event)
            if drop is not None:
                self.owner.call(lambda: self.node_drag.commit(drop))
            self.update_cursor(event.position().toPoint())
            event.accept()
            return
        self.press_node = None
        super().mouseReleaseEvent(event)
        self.update_cursor(event.position().toPoint())

    def mouseDoubleClickEvent(self, event):
        """按钮双击仍按独立点击处理，不把事件交给正文的双击编辑入口。"""
        if self._fold_pressed or isinstance(self.itemAt(event.position().toPoint()),
                                           (FoldControlItem, SubmapControlItem)):
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
            self._show_context_menu(event.pos(), event.globalPos())

    def _show_context_menu(self, position, global_position):
        """仅在确定为点击后选择菜单目标，右键拖动始终保留原有选择。"""
        item = self.itemAt(position)
        if isinstance(item, (FoldControlItem, SubmapControlItem)):
            item = item.node_item
        if item is not None and isinstance(item, NodeItem) and not item.isSelected():
            self.owner.select_nodes([item.node.id])
        self.owner.context_menu(global_position)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.press_node is not None:
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
            ids = self.owner.selected_node_ids()
            if ids:
                current = self.owner.node_items[ids[0]].sceneBoundingRect().center()
                dx, dy = directions[event.key()]
                if self.owner._virtualized:
                    target = self.owner.layout_result.geometry.nearest(ids[0], dx, dy)
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
