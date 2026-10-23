"""交互节点与临时文本编辑器；静态绘制复用渲染包，修改交回宿主引擎。"""

from __future__ import annotations

from dataclasses import replace
from weakref import ref

from mindmap_layouts import resolve_tree_spacing
from mindmap_qt_render.items import NodeItem as StaticNodeItem
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetricsF, QPainterPath, QPen, QTextCursor, QTextOption
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsTextItem,
    QStyle,
    QStyleOptionGraphicsItem,
)
from shiboken6 import isValid

FOLD_HOVER_PADDING = 10.0
FOLD_HIDE_DELAY = 200


class FoldControlItem(QGraphicsObject):
    """节点外侧的折叠按钮；不参与正文选择、拖动、文字测量或图形导出。

    图元是节点的子项，自动跟随位置、缩放和拖动后的重排动画。鼠标由画布视图
    统一路由，图元自身不抢焦点、不抓取鼠标，连接正文的透明通道仅维持悬停。
    """

    def __init__(self, node_item):
        super().__init__(node_item)
        # Qt 的 parentItem 已负责按钮随父节点销毁；Python 只反向借用父节点。
        # 若这里强引用 node_item，会和 node_item.fold_control 形成循环，使已从
        # 场景移除的父子图元滞留到 GC；GC 可能在另一个图元的 Qt 构造回调中重入，
        # 此时再析构旧的 QGraphicsObject 父子树会触发原生访问冲突。
        self._node_item_ref = ref(node_item)
        self.direction = "right"
        self._rect = QRectF()
        self._bounds = QRectF()
        self._hit_path = QPainterPath()
        self.setZValue(1)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.sync()

    @property
    def node_item(self) -> NodeItem | None:
        """借用仍有效的父节点；外部保留按钮包装对象也不延长父节点生命周期。

        父节点的 Python 或 Qt 对象已经释放时返回 None，迟到的显示、命中及权限
        查询直接忽略，禁止再访问已经失效的 PySide 包装对象。
        """
        item = self._node_item_ref()
        return item if item is not None and isValid(item) else None

    def sync(self) -> None:
        """同步当前节点的数量、方向及可见性，不读取或复制整份引擎文档。

        正文视觉可在不同节点之间共享，且同为非叶子时增加子节点未必更换视觉；
        因此宿主每次同步节点实体后均调用本接口，计数只取当前 children 的长度。
        """
        item = self.node_item
        if item is None:
            return
        self.direction = item.owner._fold_direction(item.node.id) if item.owner is not None else "right"
        rect = item.visual.fold_rect(
            self.direction, child_count=0 if item.node.expanded else len(item.node.children)
        )
        if rect != self._rect:
            self.prepareGeometryChange()
            self._rect = rect
            if self.direction == "left":
                bridge = QRectF(-4, 0, 4, item.visual.height)
            elif self.direction == "down":
                bridge = QRectF(0, item.visual.height, item.visual.width, 4)
            else:
                bridge = QRectF(item.visual.width, 0, 4, item.visual.height)
            self._hit_path = QPainterPath()
            # 通道覆盖正文整条外缘，徽标四角也只维持悬停，使斜向移入圆圈时
            # 不会经过命中空洞；实际点击仍由 contains_button 检查圆角轮廓。
            self._hit_path.addRect(rect)
            self._hit_path.addRect(bridge)
            self._bounds = self._hit_path.boundingRect().adjusted(-1, -1, 1, 1)
        self.setToolTip("收起子节点" if item.node.expanded else f"展开 {len(item.node.children)} 个子节点")
        self.sync_visibility()
        self.update()

    def sync_visibility(self) -> None:
        """同步绘制状态；悬停移出后的 200 毫秒内保留按钮，收起计数始终显示。"""
        item = self.node_item
        if item is None:
            return
        self.setVisible(bool(item.owner is not None and not item.owner._disposed and item.node.children and (
            not item.node.expanded or item.hovered or item.isSelected() or item._fold_hide_pending
        )))

    def hover_distance(self, scene_position, scale: float) -> float | None:
        """返回指针到按钮或出线短段的屏幕距离，不依赖按钮是否显示。

        本接口只负责悬停识别，不改变文档，也不把连线变成折叠点击区域。内置树的
        分叉位于父子间隔前半段，热区覆盖节点出口到半间距及分叉附近；向左、向下
        使用相同几何镜像。线两侧与按钮周围额外保留 10 个视口像素，缩放只换算
        检测尺寸，不重新测量节点或建立整图索引。透明连接通道仍维持斜向移动。
        """
        item = self.node_item
        if item is None or item.owner is None or item.owner._disposed or not item.node.children:
            return None
        local = self.mapFromScene(scene_position)
        if self._hit_path.contains(local):
            return 0.0

        def distance(rect):
            dx = max(rect.left() - local.x(), 0.0, local.x() - rect.right())
            dy = max(rect.top() - local.y(), 0.0, local.y() - rect.bottom())
            return (dx * dx + dy * dy) ** 0.5

        closest = distance(self._rect)
        if item.node.expanded:
            config = item.owner.document_view.layout
            # 内置布局的分叉热区跟随实际父子间距，数值自定义后也不伸入子节点。
            # 扩展算法沿用原有紧凑标记，不解释其专有 options。
            reach = (resolve_tree_spacing(config)[0] / 2 if config.algorithm_id == "tree"
                     else 16.0 if config.compact else 32.0)
            if self.direction == "left":
                stem = QRectF(-reach, item.visual.height / 2, reach, 0)
            elif self.direction == "down":
                stem = QRectF(item.visual.width / 2, item.visual.height, 0, reach)
            else:
                stem = QRectF(item.visual.width, item.visual.height / 2, reach, 0)
            closest = min(closest, distance(stem))
            document = item.owner.document_view
            if document.layout.direction == "both" and item.node.id == document.root_id:
                # 根按钮保持原来的右侧位置，左侧实际分支附近也能唤出同一个按钮。
                closest = min(closest, distance(QRectF(-reach, item.visual.height / 2, reach, 0)))
        pixels = closest * scale
        return pixels if pixels <= FOLD_HOVER_PADDING else None

    def can_activate(self) -> bool:
        """实时检查修改权限；编辑、拖动或释放组件后不得提交折叠命令。"""
        item = self.node_item
        owner = item.owner if item is not None else None
        return bool(owner is not None and not owner._disposed and owner.editable
                    and owner.editor is None and not owner.view.node_drag.active
                    and item.node.children)

    def button_rect(self) -> QRectF:
        """返回与绘制一致的按钮矩形；坐标相对于所属节点，透明连接通道不在其中。"""
        return QRectF(self._rect)

    def contains_button(self, scene_position) -> bool:
        """以场景坐标检查实际圆角按钮，防止透明悬停通道被当成点击或拖动入口。"""
        if self.node_item is None:
            return False
        path = QPainterPath()
        path.addRoundedRect(self._rect, 10, 10)
        return self.isVisible() and path.contains(self.mapFromScene(scene_position))

    def boundingRect(self):
        """边界覆盖圆角按钮描边及透明悬停通道，不改变父节点正文边界。"""
        return QRectF(self._bounds)

    def shape(self):
        """返回按钮与连接通道的联合轮廓；实际点击仍由 contains_button 单独判定。"""
        return self._hit_path

    def paint(self, painter, option, widget=None):
        """借用统一描画入口及场景当前底色，仅绘制按钮，不修改任何文档状态。"""
        item = self.node_item
        if item is None:
            return
        item.visual.paint_fold_control(
            painter, item.node, rect=self._rect, background=self.scene().backgroundBrush().color()
        )


class SubmapControlItem(QGraphicsObject):
    """叶子入口的常驻页面按钮；仅画布创建，静态渲染与导出不包含此控件。"""

    def __init__(self, node_item):
        super().__init__(node_item)
        self._node_item_ref = ref(node_item)
        self._rect = QRectF()
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setZValue(2)
        self.setToolTip("进入子导图")
        self.sync()

    @property
    def node_item(self):
        item = self._node_item_ref()
        return item if item is not None and isValid(item) else None

    def sync(self):
        item = self.node_item
        if item is None:
            return
        rect = QRectF(item.visual.width + 6, (item.visual.height - 22) / 2, 22, 22)
        if rect != self._rect:
            self.prepareGeometryChange()
            self._rect = rect
        self.setVisible(bool(item.node.submap_id))
        self.update()

    def can_activate(self):
        """只读仍允许导航；文字编辑、拖动和销毁期间的迟到点击保持原位。"""
        item = self.node_item
        return bool(item and item.owner and not item.owner._disposed and item.node.submap_id
                    and item.owner.editor is None and not item.owner.view.node_drag.active)

    def boundingRect(self):
        return self._rect

    def paint(self, painter, option, widget=None):
        item = self.node_item
        if item is None:
            return
        colors = item.owner._ui_colors
        painter.setPen(QPen(QColor(colors.accent if colors else item.theme.accent), 1.3))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        rect = self._rect.adjusted(5, 3, -5, -3)
        painter.drawRoundedRect(rect, 2, 2)
        painter.drawLine(rect.left() + 3, rect.top() + 6, rect.right() - 3, rect.top() + 6)
        painter.drawLine(rect.left() + 3, rect.top() + 10, rect.right() - 3, rect.top() + 10)


class NodeItem(StaticNodeItem):
    """保持原节点构造接口；有宿主时转发双击编辑，导出图元无需创建该交互对象。"""

    def __init__(self, node, visual, theme, owner=None):
        super().__init__(node, visual, theme, controls=False)
        self.owner = owner
        self.hovered = False
        self._fold_hide_pending = False
        self.fold_control = FoldControlItem(self) if owner is not None else None
        self.submap_control = SubmapControlItem(self) if owner is not None else None

    def set_hovered(self, hovered: bool, *, immediate: bool = False) -> None:
        """进入立即显示，离开延迟 200 毫秒隐藏；再次进入取消待执行的隐藏。

        immediate 用于手势中断或文档刷新，立即清除旧悬停状态。定时器由视图统一
        管理，节点只保存临时标记；重复的鼠标移动不会不断重启隐藏倒计时。
        """
        changed = self.hovered != hovered
        self.hovered = hovered
        if hovered or immediate:
            if self.owner is not None:
                self.owner.view._cancel_fold_hide(self)
        elif changed and self.fold_control is not None and self.fold_control.isVisible():
            if self.node.expanded and not self.isSelected():
                self.owner.view._defer_fold_hide(self)
        if self.fold_control is not None and (changed or immediate):
            self.fold_control.sync_visibility()

    def sync_fold_control(self) -> None:
        """宿主同步实体及布局后更新外侧控件，保持正文边界与拖动几何独立。"""
        if self.owner is not None and self._fold_hide_pending and (not self.node.children or not self.node.expanded):
            self.owner.view._cancel_fold_hide(self)
        if self.fold_control is not None:
            self.fold_control.sync()
        if self.submap_control is not None:
            self.submap_control.sync()

    def itemChange(self, change, value):
        result = super().itemChange(change, value)
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            control = getattr(self, "fold_control", None)
            if control is not None:
                control.sync_visibility()
        return result

    def mouseDoubleClickEvent(self, event):
        if self.owner:
            self.owner.begin_edit(self.node.id)
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)


class NodeEditor(QGraphicsTextItem):
    """无边框原地编辑器；草稿尺寸只影响画面，提交时才生成一次引擎命令。

    编辑器复用节点的字体、正文位置和最大文本宽度，以真实文本布局调整圆角
    外框。中文候选属于临时草稿，参与尺寸计算但不写入节点或撤销历史。
    """

    def __init__(self, owner, node, visual):
        super().__init__()
        self.owner, self.node_id = owner, node.id
        self.composing = False
        self.finishing = False
        self.base_layout = owner.layout_result
        self._base_visual = visual
        self.preview_visual = visual
        self._max_width = float(node.style.get("width", 280))
        self._preedit = ""
        self._syncing_geometry = False
        self.setZValue(20)
        self.setFont(visual.font)
        self.setDefaultTextColor(visual.text_color)
        self.setPlainText(node.text)
        self.document().setDocumentMargin(0)
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.document().setDefaultTextOption(option)
        self.setTextWidth(visual.text.textWidth())
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        cursor = self.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        self.setTextCursor(cursor)
        self.document().contentsChanged.connect(self.sync_geometry)
        self.document().documentLayout().documentSizeChanged.connect(self.sync_geometry)

    def paint(self, painter, option, widget=None):
        """仅移除 Qt 默认的虚线焦点框，保留真实焦点、光标及文字选择绘制。

        拷贝样式参数再清除绘制标记，避免影响其他图元；键盘和输入法仍由原生
        文本控件处理。编辑状态统一由节点现有的蓝色圆角选中边框表达。
        """
        text_option = QStyleOptionGraphicsItem(option)
        text_option.state &= ~QStyle.StateFlag.State_HasFocus
        super().paint(painter, text_option, widget)

    def sync_geometry(self, *_args, force=False):
        """按当前正文及候选实时换行、调整节点，不复制或修改引擎中的文档。

        setTextWidth 会同步发送文档尺寸信号，使用重入保护合并这轮测量。图片
        保留位置，标签和备注跟随正文高度平移；共享的正式 NodeVisual 始终只读。
        """
        if (self._syncing_geometry or self.finishing or self.owner._disposed
                or self.owner.editor is not self):
            return
        self._syncing_geometry = True
        try:
            text = self.toPlainText()
            if self._preedit:
                # QTextCursor 的位置按 UTF-16 计数，不能直接当成 Python 字符下标；
                # 先换算位置，避免表情符号之后输入中文时把候选宽度算到错误行。
                prefix = text.encode("utf-16-le")[:self.textCursor().position() * 2]
                position = len(prefix.decode("utf-16-le"))
                text = text[:position] + self._preedit + text[position:]
            metrics = QFontMetricsF(self.font())
            natural = max((metrics.horizontalAdvance(line) for line in text.splitlines()), default=40)
            width = max(36, min(self._max_width, natural + 3))
            if self.textWidth() != width:
                self.setTextWidth(width)
            base = self._base_visual
            height = max(metrics.height(), self.document().size().height())
            delta = height - max(metrics.height(), base.text.size().height())
            tags = [(value, rect.translated(0, delta)) for value, rect in base.tags]
            node_width = max([width + 28, *(rect.right() + 14 for _, rect in tags)])
            if base.image is not None:
                node_width = max(node_width, base.image_rect.right() + 14)
            visual = replace(base, width=node_width, height=base.height + delta,
                             text=self.document(), tags=tags)
            self.preview_visual = visual
            self.owner._update_edit_preview(self, force=force)
        finally:
            self._syncing_geometry = False

    def inputMethodEvent(self, event):
        self._preedit = event.preeditString()
        self.composing = bool(self._preedit)
        super().inputMethodEvent(event)
        # 只有候选改变时不一定发出正文变更信号，仍需按预编辑布局更新外框。
        self.sync_geometry()

    def keyPressEvent(self, event):
        if self.composing and event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Tab,
            Qt.Key.Key_Escape,
        ):
            # 输入法通常先消费候选确认键；仍送达图元的按键不得插入换行或结束编辑。
            # 预编辑文本的最终提交/取消只根据后续 QInputMethodEvent 更新。
            event.accept()
        elif event.key() == Qt.Key.Key_Escape:
            self.owner.finish_edit(commit=False)
            event.accept()
        elif (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab)
            and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.owner.finish_edit(commit=True)
            event.accept()
        else:
            super().keyPressEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        # 弹出菜单只是临时借用键盘焦点，关闭后仍回到当前文字草稿。
        # 尤其导图设置菜单需要在编辑期间保留禁用状态，不能因打开菜单而自动
        # 提交正文并解锁文档设置；真正切换到其他控件时仍沿用失焦提交规则。
        if event.reason() == Qt.FocusReason.PopupFocusReason:
            return
        if not self.finishing:
            # 焦点事件返回后再移除图元，避免在 Qt 虚函数调用栈中销毁编辑器。
            QTimer.singleShot(
                0, lambda: self.owner.finish_edit(commit=True) if self.owner.editor is self else None
            )
