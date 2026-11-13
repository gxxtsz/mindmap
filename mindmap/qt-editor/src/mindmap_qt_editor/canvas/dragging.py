"""节点拖动的内部控制器：固定目标、吸附提示、放下后重排和边缘滚动。

拖动期间只读起点快照，不移动其他节点，也不展开折叠主题。松手后才独立试算、
提交一次事务，并对正式布局播放动画；动画不改写文档、历史或导出几何。
"""

from __future__ import annotations

from collections import ChainMap
from dataclasses import dataclass, replace

from mindmap_core import (
    BranchPath,
    Curve,
    LayoutResult,
    MindMapDocument,
    MindMapEngine,
    MindMapForestView,
    NodeGraph,
    Rect,
)
from mindmap_core.layouts.graph import move_graph_nodes
from mindmap_qt_render.rendering import branch_path
from PySide6.QtCore import (
    QEasingCurve,
    QElapsedTimer,
    QObject,
    QPoint,
    QPointF,
    QRectF,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import QPainterPath

from .branch_drag import encode_branch_drag
from .drag_preview import _DragLayer
from .drop_targets import _distance, _DropProposal, _DropResolver, _qrect

LEAVE_DISTANCE = 36.0
ANIMATION_DURATION = 140
# 千节点实测单帧已超过 16 毫秒；大图直接展示最终布局，避免动画阻塞后续输入。
ANIMATION_NODE_LIMIT = 500
SCROLL_MARGIN = 32.0
SCROLL_SPEED = 480.0


@dataclass
class _DragSnapshot:
    """只读手势起点；图元内容和命中区域在整次拖动中均以它为准。"""

    document: MindMapDocument
    visuals: dict
    layout: LayoutResult


@dataclass
class _PreparedDrop:
    """先结束鼠标捕获，再执行正式命令；保留手势起点快照用于方向与空操作判定。"""

    proposal: _DropProposal
    document: MindMapDocument
    selection: list[str]
    content_version: int
    focus_id: str | None
    start_nodes: dict[str, Rect]


@dataclass
class _Reflow:
    """提交后的纯展示状态，与拖动影像分开管理，允许正式刷新随时结束动画。"""

    layout: LayoutResult
    start_nodes: dict[str, Rect]
    parents: dict[str, str]


def _shift_branch(branch: Curve | BranchPath, parent_delta, child_delta):
    """按父子位移变形最终连线，保留注入布局及现有分支样式的路径语义。

    动画中父子矩形可以重叠或穿过彼此，不能再次调用要求合法树几何的正式路由。
    改为沿最终出线方向插值两端位移：端点跟随节点，控制点平滑过渡，箭头整体
    跟随子节点。结束时直接使用原始布局路径，避免累计误差或改写正式布局。
    """

    def shift(point, weight):
        return (
            point[0] + parent_delta[0] * (1 - weight) + child_delta[0] * weight,
            point[1] + parent_delta[1] * (1 - weight) + child_delta[1] * weight,
        )

    if isinstance(branch, Curve):
        return replace(
            branch, points=tuple(shift(point, index / 3) for index, point in enumerate(branch.points))
        )
    start, end = branch.start, branch.end
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = dx * dx + dy * dy

    def warp(point):
        weight = (
            max(0, min(1, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length))
            if length
            else 0.5
        )
        return shift(point, weight)

    return replace(
        branch,
        start=shift(start, 0),
        end=shift(end, 1),
        commands=tuple(
            replace(command, points=tuple(map(warp, command.points))) for command in branch.commands
        ),
        arrow=tuple(shift(point, 1) for point in branch.arrow) if branch.arrow else None,
    )


def _changes(document: MindMapDocument, proposal: _DropProposal) -> bool:
    """先比较结构及有效方向，再固化自动方向，避免空拖动制造历史记录。"""
    roots = set(proposal.roots)
    parent = document.nodes[proposal.parent_id]
    children = [key for key in parent.children if key not in roots]
    children[proposal.index : proposal.index] = proposal.roots
    if children != parent.children:
        return True
    if document.layout.direction == "both" and proposal.parent_id == document.root_id:
        sides = _root_sides(document)
        return any(sides[key] != proposal.direction for key in proposal.roots)
    return False


def _root_sides(document):
    """只读取一级分支的有效方向，固定自动方向时不需要遍历每棵子树。"""
    counts, result = {"left": 0, "right": 0}, {}
    for key in document.root.children:
        side = document.nodes[key].direction or min(counts, key=counts.get)
        counts[side] += 1
        result[key] = side
    return result


def _apply_move(engine: MindMapEngine, proposal: _DropProposal, document: MindMapDocument) -> bool:
    """在放下试算和正式引擎上执行相同事务，方向、排序和展开状态一起提交或回滚。

    一级分支自动方向与根的子节点顺序相关。先固定未移动一级分支的原有效方向，
    再移动并设置目标方向，避免被移动分支带动其他分支换边。现有引擎的 before/after
    会继承参照分支方向，因此移动根的最终方向必须在 move_nodes 之后设置。
    """
    if not _changes(document, proposal):
        return False
    moving = set(proposal.roots)
    siblings = [key for key in document.nodes[proposal.parent_id].children if key not in moving]
    both = document.layout.direction == "both" and getattr(document, "kind", None) != "inspiration"
    touches_root = proposal.parent_id == document.root_id or any(
        engine.parent_id(key) == document.root_id for key in proposal.roots
    )
    with engine.transaction("拖动节点"):
        if both and touches_root:
            sides = _root_sides(document)
            for key in document.root.children:
                if key not in moving and document.nodes[key].direction != sides[key]:
                    engine.update_node(key, direction=sides[key])
        if proposal.index < len(siblings):
            engine.move_nodes(proposal.roots, siblings[proposal.index], "before")
        elif siblings:
            engine.move_nodes(proposal.roots, siblings[-1], "after")
        else:
            engine.move_nodes(proposal.roots, proposal.parent_id, "inside")
        # move_nodes 在同一事务中展开目标父节点；悬停不运行此流程，因此折叠主题
        # 只在成功放下后展开。聚焦范围以外的折叠祖先仍保持原样。
        if both and proposal.parent_id == document.root_id:
            for key in proposal.roots:
                engine.update_node(key, direction=proposal.direction)
    return True


@dataclass(frozen=True)
class _GraphDrop:
    """鼠标释放后等待提交的图节点坐标，绑定按下时的图页及内容版本。"""

    page_id: str
    content_version: int
    graph: NodeGraph
    positions: dict[str, tuple[float, float]]


class NodeDragController(QObject):
    """内部鼠标及跨控件原生拖放共用的状态机，所有预览和定时器由视图拥有。"""

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.owner = view.owner
        self.ghost = None
        self.target = None
        self._base = None
        self._source = None
        self._external_mime = None
        self._external_forbidden = set()
        self._resolver = None
        self._reflow = None
        self._pointer = QPoint()
        self._last_scene_point = None
        self._last_scale = None
        self._offset = QPointF()
        self._graph_drag = None
        self._display_nodes = {}
        self._content_version = 0
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(ANIMATION_DURATION)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._animate)
        self._animation.finished.connect(self._settle_reflow)
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(16)
        self._scroll_timer.timeout.connect(self._scroll)
        self._scroll_clock = QElapsedTimer()
        self._scroll_remainder = QPointF()

    @property
    def active(self):
        return self.ghost is not None or self._graph_drag is not None

    @property
    def external_active(self):
        """是否存在尚未提交的原生拖入预览；已提交的重排动画不计入。"""
        return self.active and self._external_mime is not None

    def begin(self, origin):
        """越过系统阈值后冻结源几何与选择；只读、根节点和聚焦中心在此再次校验。"""
        if not self.owner.editable or self.owner.editor is not None or self.owner._disposed:
            return False
        if not all(guard() for guard in tuple(self.owner._navigation_guards)):
            self.owner.error_occurred.emit("请先完成输入法候选，再拖动主题")
            return False
        # 下一次操作开始前收束上次放下动画，避免拿中间帧作为新的命中几何。
        self._settle_reflow()
        if self.owner.graph is not None:
            keys = self.owner.selected_node_ids()
            if not keys:
                return False
            # 与树拖动共用阈值、捕获、边缘滚动和取消时机。图内只计算位置差量，
            # 预览移动原图元并随手势重算相连边，无需创建另一套拖动影像。
            self._graph_drag = (self.owner.current_page_id, self.owner.graph, self.owner.layout_result, keys)
            self._origin = QPointF(origin)
            self._offset = QPointF()
            return True
        # 组件持有的正式展示文档本身就是引擎快照；正式内容改变时会先中止手势，
        # 因而可以只读复用它，保持未变化节点、视觉和图元之间的对象一致性。
        document = self.owner._document
        selection = self.owner.selected_node_ids()
        resolver = _DropResolver(document, self.owner.layout_result, selection, self.owner.focus_id)
        if not resolver.valid:
            return False
        self.owner._cancel_branch_menu()
        # 正式图元已经持有测量结果，拖动影像只需要可见节点，无需再次测量整图。
        visuals = {key: item.visual for key, item in self.owner.node_items.items()}
        snapshot = _DragSnapshot(document, visuals, self.owner.layout_result)
        nodes = {
            key: snapshot.layout.nodes[key] for key in self.owner.node_items if key in resolver.moving
        }
        branches = [
            _shift_branch(item._branch, (item.pos().x(), item.pos().y()), (item.pos().x(), item.pos().y()))
            for key, item in self.owner.branch_items.items()
            if key in resolver.moving and resolver.parents.get(key) in resolver.moving
        ]
        self._start_preview(snapshot, snapshot, resolver, selection, origin, nodes, branches)
        return True

    def _start_preview(self, target, source, resolver, selection, origin, nodes, branches):
        """用独立的源影像与冻结目标建立共用绘制层，不执行布局或修改文档。"""
        self._base, self._source = target, source
        self._selection, self._resolver = selection, resolver
        resolver.rebuild(target.layout.nodes, scale=self.view.transform().m11())
        self._origin = QPointF(origin)
        self._last_scene_point = self._last_scale = None
        self._ghost_nodes, self._ghost_branches = nodes, branches
        self._ghost_node_bounds = {key: _qrect(rect) for key, rect in nodes.items()}
        # 水平、竖直直线的原始包围框可能宽或高为零，扩展后才能正确参与可见性相交判断。
        self._ghost_branch_bounds = [branch_path(branch).boundingRect().adjusted(-4, -4, 4, 4)
                                     for branch in branches]
        self._ghost_bounds = QRectF()
        for rect in self._ghost_node_bounds.values():
            self._ghost_bounds = self._ghost_bounds.united(rect)
        self._offset = QPointF()
        self.ghost = _DragLayer(self)
        self.ghost.sync()
        self.owner.scene.addItem(self.ghost)
        self._dim_sources()

    def begin_external(self, mime, position) -> bool:
        """进入当前视口时建立原生拖放预览，返回是否接受本文件的移动载荷。

        mime 只借用本次调用；控制器复制标识载荷并自行持有，避免原生 QDrag
        释放数据后仍被滚动定时器访问。源树在进入时测量和布局一次，目标页不动。
        position 是视口逻辑像素 QPoint；源影像只绘制当前视口可见部分。
        不切换页面或选择，不运行正式事务；异常由视图交给宿主错误边界。
        """
        if not self.owner.can_receive_branch_drag(mime):
            self.cancel_external()
            return False
        data = self.owner._branch_drag_data(mime)
        self.cancel()
        self.owner._cancel_branch_menu()
        context = self.owner.current_page_id, self.owner.current_inspiration_root_id
        target = _DragSnapshot(self.owner._document,
                               {key: item.visual for key, item in self.owner.node_items.items()},
                               self.owner.layout_result)
        resolver = _DropResolver(target.document, target.layout, (), self.owner.focus_id,
                                 moving_roots=data.node_ids)
        if not resolver.valid:
            return False
        if all(key in target.document.nodes for key in data.node_ids):
            source = target
            nodes = {key: target.layout.nodes[key] for key in self.owner.node_items if key in resolver.moving}
            branches = [
                _shift_branch(item._branch, (item.pos().x(), item.pos().y()), (item.pos().x(), item.pos().y()))
                for key, item in self.owner.branch_items.items()
                if key in resolver.moving and resolver.parents.get(key) in resolver.moving
            ]
        else:
            book = self.owner._book_document
            # 独立的区域外壳借用节点与资源，配置采用目标画布；不改原坐标、外观或折叠状态。
            area = replace(book.inspiration, root_ids=list(data.node_ids),
                           positions={key: (0, index * 80) for index, key in enumerate(data.node_ids)},
                           layout=target.document.layout, theme=target.document.theme)
            document = MindMapForestView(
                area, {key: book.nodes[key] for root in data.node_ids for key in book.descendants(root)},
                book.assets, book.metadata)
            # 使用单节点工厂入口，不让独立影像的测量清理当前目标页的正式缓存。
            visuals = {key: self.owner.factory.visual(node, document.theme, document,
                                                      is_root=key in document.root_ids,
                                                      detail_page_resolver=self.owner.engine.resolve_detail_page)
                       for key, node in document.nodes.items()}
            sizes = {key: (visual.width, visual.height) for key, visual in visuals.items()}
            layout = self.owner.layout_registry.layout(document, sizes)
            source = _DragSnapshot(document, visuals, layout)
            nodes, branches = dict(layout.nodes), list(layout.branches.values())
        # 注入布局可能回调宿主修改内容或导航；旧源影像不能安装到已经改变的目标页。
        if (context != (self.owner.current_page_id, self.owner.current_inspiration_root_id)
                or not self.owner.can_receive_branch_drag(mime)):
            return False
        forbidden = {key for root in data.node_ids
                     for key in self.owner._book_document.owned_descendants(root)}
        anchor = source.layout.nodes[data.node_ids[0]]
        self._start_preview(target, source, resolver, self.owner.selected_node_ids(),
                            QPointF(*anchor.center), nodes, branches)
        self._external_mime = encode_branch_drag(data)
        self._external_mime.setParent(self)
        # 内容版本在手势期间冻结，源分支及其附属页面的禁入集合只需计算一次。
        self._external_forbidden = forbidden
        self.update(position)
        return self.external_active

    def update_external(self, mime, position) -> bool:
        """按原生移动事件更新共用落点，返回当前是否存在可提交的结构目标。

        mime 只借用本次事件，position 为视口逻辑像素 QPoint。载荷失效时取消；
        单纯无落点则保留影像以便继续移动，故返回 False 不一定代表手势已经结束。
        """
        if not self.external_active:
            return False
        data = self.owner._branch_drag_data(mime)
        if data is None or data != self.owner._branch_drag_data(self._external_mime):
            self.cancel_external()
            return False
        self.update(position)
        return self.target is not None

    def cancel_external(self) -> None:
        """只取消未提交的原生预览；拖动结束通知不会提前结束已提交的重排动画。"""
        if self.external_active:
            self.cancel()

    def drop_external(self, mime, position) -> bool:
        """按释放位置复查落点，提交跨区域迁入后播放与内部拖动相同的重排动画。"""
        if not self.update_external(mime, position):
            self.cancel_external()
            return False
        drop = self.finish(position)
        if drop is None:
            return False
        proposal = drop.proposal
        changed = self.owner.receive_branch_drag(mime, proposal.parent_id,
                                                 index=proposal.index, direction=proposal.direction)
        if (changed and not self.owner._disposed and self.owner.editable
                and self._content_version == drop.content_version + 1
                and self.owner.focus_id == drop.focus_id):
            self._start_reflow(drop.start_nodes)
        return changed

    def _dim_sources(self):
        """原位置保留淡色源分支作为参照，其他主题和连线完全保持原貌。"""
        for key in self.owner.node_items:
            if key in self._resolver.moving:
                self.owner.node_items[key].setOpacity(0.18)
        for key in self.owner.branch_items:
            if key in self._resolver.moving:
                self.owner.branch_items[key].setOpacity(0.12)

    def _connection_hint(self):
        """仅为接入子节点绘制预连接线，同级排序由独立的间隙短条表达。"""
        path = QPainterPath()
        target = self.target
        if target is None or target.position != "inside":
            return path
        nodes = self._base.layout.nodes
        parent = nodes.get(target.proposal.parent_id)
        if parent is None:
            return path
        side = target.proposal.direction or self._resolver.sides[target.proposal.parent_id]
        start = QPointF(
            parent.x if side == "left" else parent.right if side == "right" else parent.center[0],
            parent.bottom if side == "down" else parent.center[1],
        )
        for key in self._resolver.roots:
            rect = self._ghost_nodes.get(key)
            if rect is None:
                continue
            end = (
                QPointF(
                    rect.right if side == "left" else rect.x if side == "right" else rect.center[0],
                    rect.y if side == "down" else rect.center[1],
                )
                + self._offset
            )
            path.moveTo(start)
            if side == "down":
                middle = (start.y() + end.y()) / 2
                path.cubicTo(QPointF(start.x(), middle), QPointF(end.x(), middle), end)
            else:
                middle = (start.x() + end.x()) / 2
                path.cubicTo(QPointF(middle, start.y()), QPointF(middle, end.y()), end)
        return path

    def _hint_text(self):
        """直接描述当前落点的结构结果，提示方向与布局排序轴一致。"""
        if self.target is None:
            return ""
        if self.target.position == "inside":
            return "＋ 作为子主题"
        if self._resolver.down:
            return "← 插到左侧" if self.target.position == "before" else "→ 插到右侧"
        return "↑ 插到前面" if self.target.position == "before" else "↓ 插到后面"

    def _insertion_hint(self):
        """返回同级插入短条的场景矩形；位置与命中解耦，不移动正式节点。

        默认使用 40×3 个视口逻辑像素，与目标边缘保留 8 像素空隙。缩小时按
        主题宽度收短；间隙不足时居中并减薄，避免压到相邻主题。双向布局只考虑
        同侧兄弟，向下布局把排序轴转为水平方向，提示短条相应转为竖向。
        """
        target = self.target
        if target is None or target.position == "inside":
            return QRectF()
        nodes = self._base.layout.nodes
        rect = nodes[target.reference]
        scale = self.view.transform().m11()
        down = self._resolver.down
        before = target.position == "before"
        thickness = 3 / scale
        clearance = 8 / scale
        siblings = self._resolver.remaining[target.proposal.parent_id]
        index = siblings.index(target.reference)
        neighbors = reversed(siblings[:index]) if before else iter(siblings[index + 1 :])
        side = self._resolver.sides[target.reference]
        neighbor = next(
            (nodes[key] for key in neighbors if key in nodes and self._resolver.sides[key] == side), None
        )
        edge = (rect.x if before else rect.right) if down else (rect.y if before else rect.bottom)
        if neighbor is not None:
            other = (
                (neighbor.right if before else neighbor.x)
                if down
                else (neighbor.bottom if before else neighbor.y)
            )
            gap = edge - other if before else other - edge
            if gap > 0:
                thickness = min(thickness, gap / 2)
                clearance = min(clearance, (gap - thickness) / 2)
        axis = edge + (-1 if before else 1) * (clearance + thickness / 2)
        span = rect.height if down else rect.width
        length = max(thickness * 2, min(40 / scale, span * 0.6))
        if down:
            return QRectF(axis - thickness / 2, rect.center[1] - length / 2, thickness, length)
        return QRectF(rect.center[0] - length / 2, axis - thickness / 2, length, thickness)

    def update(self, position):
        """只更新稳定落点、影像和滚动；悬停不试算文档、不测量、不启动布局动画。"""
        if not self.active:
            return
        if self._graph_drag is not None:
            self._pointer = QPoint(position)
            offset = self.view.mapToScene(position) - self._origin
            if offset != self._offset:
                self._offset = offset
                _, graph, base, keys = self._graph_drag
                result = move_graph_nodes(base, graph.edges, self._graph_positions(base, keys))
                self.owner._show_edit_layout(result)
            self._update_edge_scroll(self.view.viewport().rect().contains(position))
            return
        if self.external_active and not self.owner.can_receive_branch_drag(self._external_mime):
            self.cancel_external()
            return
        point = self.view.mapToScene(position)
        scale = self.view.transform().m11()
        moved = position != self._pointer or point != self._last_scene_point or scale != self._last_scale
        self._pointer = QPoint(position)
        self._offset = point - self._origin
        inside = self.view.viewport().rect().contains(position)
        current = self.target
        if not moved:
            # 重复移动及释放使用同一命中；滚动或缩放则按新场景坐标和倍率重算。
            candidate, direct = current, False
        else:
            candidate, direct = self._resolver.resolve(point, scale) if inside else (None, True)
            blocked = getattr(self._resolver, "blocked_submap", None) if inside else None
            if blocked is not None and blocked != getattr(self, "_blocked_submap", None):
                self.owner.error_occurred.emit("此节点已有子导图，请进入子导图添加内容")
            self._blocked_submap = blocked
        # 明确进入正文或排序区时立即按当前意图切换；只有离开所有明确落区后的
        # 外侧吸附使用 36 像素退出距离。旧排序锁定不能跨进正文继续提交同级移动。
        if (
            current is not None
            and not direct
            and self._resolver.allows(current, point)
            and _distance(point, current.region) * scale <= LEAVE_DISTANCE
        ):
            candidate = current
        if candidate is not None and not _changes(self._base.document, candidate.proposal):
            candidate = None
        if (candidate is not None and self.external_active
                and candidate.proposal.parent_id in self._external_forbidden):
            candidate = None
        self.target = candidate
        self._offset = self.view.mapToScene(position) - self._origin
        self._last_scene_point = self.view.mapToScene(position)
        self._last_scale = scale
        self.ghost.sync()
        self._update_edge_scroll(inside)

    def _update_edge_scroll(self, inside):
        """图和树共用视口边缘滚动的计时器和速度计算。"""
        if inside and self._velocity() != QPointF():
            if not self._scroll_timer.isActive():
                self._scroll_remainder = QPointF()
                self._scroll_clock.start()
                self._scroll_timer.start()
        else:
            self._scroll_timer.stop()

    def _graph_positions(self, layout, keys):
        return {key: (layout.nodes[key].x + self._offset.x(), layout.nodes[key].y + self._offset.y())
                for key in keys}

    def _start_reflow(self, start_nodes):
        """正式提交和选择恢复结束后播放一次动画，命中与文档均使用最终布局。

        已存在的其他节点从原位置出发，移动子树从释放时的分支影像位置出发。
        折叠主题放下后才产生的新图元，从父节点当前位置附近出现并一起移动。
        """
        layout = self.owner.layout_result
        if len(layout.nodes) > ANIMATION_NODE_LIMIT:
            return
        parents = self.owner._document.parents()
        starts = {}
        bounds = layout.bounds
        # 按树顺序处理新显露的节点，确保其父节点起点已知，不依赖注入布局的字典顺序。
        for key in self.owner._document.ordered_nodes():
            if key not in layout.nodes:
                continue
            rect = layout.nodes[key]
            origin = start_nodes.get(key)
            if origin is None:
                parent = parents.get(key)
                if parent in starts:
                    before, after = starts[parent], layout.nodes[parent]
                    origin = Rect(
                        rect.x + before.x - after.x, rect.y + before.y - after.y, rect.width, rect.height
                    )
                else:
                    origin = rect
            starts[key] = Rect(origin.x, origin.y, rect.width, rect.height)
            bounds = bounds.union(starts[key])
        self._reflow = _Reflow(layout, starts, parents)
        center = self.view.mapToScene(self.view.viewport().rect()).boundingRect().center()
        self.owner.scene.setSceneRect(_qrect(bounds))
        self.view.update_navigation_rect(center=center)
        self._animate(0.0)
        self._animation.start()

    def _animate(self, progress):
        if self._reflow is None:
            return
        reflow = self._reflow
        self._display_nodes = {
            key: Rect(
                reflow.start_nodes[key].x + (rect.x - reflow.start_nodes[key].x) * progress,
                reflow.start_nodes[key].y + (rect.y - reflow.start_nodes[key].y) * progress,
                rect.width,
                rect.height,
            )
            for key, rect in reflow.layout.nodes.items()
        }
        for key, rect in self._display_nodes.items():
            self.owner.node_items[key].setPos(rect.x, rect.y)

        def delta(key):
            before, after = self._display_nodes[key], reflow.layout.nodes[key]
            return before.x - after.x, before.y - after.y

        branches = (
            reflow.layout.branches
            if progress >= 1
            else {
                key: _shift_branch(branch, delta(reflow.parents[key]), delta(key))
                for key, branch in reflow.layout.branches.items()
            }
        )
        self.owner._set_branch_items(LayoutResult(branches=branches))

    def _settle_reflow(self):
        """动画完成或被其他操作打断时收束到正式几何，不撤销已经成功提交的移动。"""
        self._animation.stop()
        reflow, self._reflow = self._reflow, None
        if reflow is None or self.owner._disposed:
            return
        for key, rect in reflow.layout.nodes.items():
            if key in self.owner.node_items:
                self.owner.node_items[key].setPos(rect.x, rect.y)
        self.owner._set_branch_items(reflow.layout)
        center = self.view.mapToScene(self.view.viewport().rect()).boundingRect().center()
        self.owner.scene.setSceneRect(_qrect(reflow.layout.bounds))
        self.view.update_navigation_rect(center=center)
        self._display_nodes = {}

    def _velocity(self):
        """边缘速度以视口逻辑像素/秒计，Qt 已处理设备像素比，不乘显示缩放。"""
        rect = self.view.viewport().rect()
        if not rect.contains(self._pointer):
            return QPointF()

        def component(value, size):
            if value < SCROLL_MARGIN:
                return -SCROLL_SPEED * (1 - value / SCROLL_MARGIN)
            if value > size - SCROLL_MARGIN:
                return SCROLL_SPEED * (1 - (size - value) / SCROLL_MARGIN)
            return 0.0

        return QPointF(
            component(self._pointer.x(), rect.width()), component(self._pointer.y(), rect.height())
        )

    def _scroll(self):
        if not self.active:
            self._scroll_timer.stop()
            return
        # 事件队列偶尔被其他操作阻塞时限制单步时间，防止恢复后突然跳过大片内容。
        elapsed = min(self._scroll_clock.restart(), 50) / 1000
        self._scroll_remainder += self._velocity() * elapsed
        for horizontal, amount in ((True, self._scroll_remainder.x()), (False, self._scroll_remainder.y())):
            distance = int(amount)
            bar = self.view.horizontalScrollBar() if horizontal else self.view.verticalScrollBar()
            bar.setValue(bar.value() + distance)
            if horizontal:
                self._scroll_remainder.setX(amount - distance)
            else:
                self._scroll_remainder.setY(amount - distance)
        self.update(self._pointer)

    def leave(self):
        """离开视口只停止边缘滚动；鼠标捕获继续允许在外部释放并取消。"""
        self._scroll_timer.stop()
        self._pointer = QPoint(-int(SCROLL_MARGIN), -int(SCROLL_MARGIN))

    def document_changed(self):
        """记录正式内容变化并中断手势，纯保存通知不调用此方法。

        释放场景捕获可能同步触发宿主回调。版本号同时保护已经取出的落点命令，
        防止捕获释放到正式提交之间发生的文档替换或编辑被旧手势继续操作。
        """
        self._content_version += 1
        self.view.cancel_interaction()

    def finish(self, position):
        """按释放坐标再次判定并记录动画起点，配对释放鼠标捕获后才提交命令。"""
        self.update(position)
        if self._graph_drag is not None:
            page, graph, base, keys = self._graph_drag
            drop = (_GraphDrop(page, self._content_version, graph, self._graph_positions(base, keys))
                    if self._offset != QPointF() and self.view.viewport().rect().contains(position) else None)
            self.cancel()
            return drop
        drop = None
        proposal = self.target.proposal if self.target is not None else None
        if self.active and proposal is not None:
            starts = {}
            if len(self._base.layout.nodes) <= ANIMATION_NODE_LIMIT:
                starts = dict(self._base.layout.nodes)
                starts.update({key: Rect(rect.x + self._offset.x(), rect.y + self._offset.y(),
                                          rect.width, rect.height)
                               for key, rect in self._ghost_nodes.items()})
            drop = _PreparedDrop(
                proposal,
                self._base.document,
                self._selection,
                self._content_version,
                self.owner.focus_id,
                starts,
            )
        self.cancel()
        return drop

    def commit(self, drop):
        """放下后独立校验布局，再一次提交和播放重排；钩子拒绝时保留正式状态。

        试算不挂真实引擎的操作钩子，也不展示候选文档。布局扩展拒绝结果时不会先
        污染正式文档。真实事务同步刷新画布并恢复选择，动画必须在这些回调后启动。
        """

        if isinstance(drop, _GraphDrop):
            if (self.owner._disposed or not self.owner.editable or self.owner.current_page_id != drop.page_id
                    or self._content_version != drop.content_version):
                return False
            graph = self.owner._read_active_graph()
            if graph != drop.graph:
                return False
            self.owner._write_active_graph(replace(graph, positions={**graph.positions, **drop.positions}))
            return True

        def valid():
            return (
                not self.owner._disposed
                and self.owner.editable
                and drop.content_version == self._content_version
                and drop.focus_id == self.owner.focus_id
            )

        if not valid() or not _changes(drop.document, drop.proposal):
            return False
        if not self.owner._can_incremental() or self.owner._incremental_layout is None:
            # 第三方布局可能拒绝合法结构，保留独立候选及原有隔离契约。
            scratch = MindMapEngine(self.owner.engine.document)
            changed_ids = []
            scratch.subscribe(lambda event: changed_ids.extend(event.node_ids))
            _apply_move(scratch, drop.proposal, drop.document)
            candidate = scratch.page_document(
                self.owner.current_page_id, inspiration_root_id=self.owner.current_inspiration_root_id)
            _, sizes = self.owner.factory.measure(
                candidate, changed_node_ids=changed_ids,
                previous_document=drop.document, previous_visuals=self.owner._visuals,
                detail_page_resolver=scratch.resolve_detail_page,
            )
            self.owner.layout_registry.layout(candidate, sizes, focus_id=drop.focus_id)
        else:
            # 受控树布局只试算变化实体及祖先。候选在原引擎的回滚日志内执行，
            # 不建立临时引擎或整图副本，也不提前执行宿主钩子和发布历史事件。
            with self.owner.engine.preview_operation() as (candidate, touched):
                _apply_move(self.owner.engine, drop.proposal, drop.document)
                if self.owner.current_page_id is not None or candidate.submaps or candidate.detail_pages:
                    candidate = candidate.page_view(
                        self.owner.current_page_id, inspiration_root_id=self.owner.current_inspiration_root_id)
                changed_sizes = {}
                for key in touched:
                    node = candidate.nodes.get(key)
                    if node is not None:
                        visual = self.owner.factory.visual(node, candidate.theme, candidate,
                                                           is_root=key == candidate.root_id,
                                                           detail_page_resolver=
                                                           self.owner.engine.resolve_detail_page)
                        changed_sizes[key] = visual.width, visual.height
                geometry = self.owner._incremental_layout.geometry.snapshot()
                geometry.sync(candidate, ChainMap(changed_sizes, self.owner._measurements), touched)
                _ = geometry.bounds
        # 外部布局或捕获释放的宿主回调有可能修改文档，不能让旧命令覆盖新内容。
        if not valid():
            return False
        try:
            changed = _apply_move(self.owner.engine, drop.proposal, drop.document)
        except Exception:
            if valid():
                self.owner.select_nodes(drop.selection)
            raise
        if (
            changed
            and not self.owner._disposed
            and self.owner.editable
            and self._content_version == drop.content_version + 1
            and self.owner.focus_id == drop.focus_id
        ):
            self.owner.select_nodes(drop.selection)
            # 选择信号也可触发宿主编辑，重新检查后才允许借用当前正式图元播放动画。
            if (
                not self.owner._disposed
                and self.owner.editable
                and self._content_version == drop.content_version + 1
                and self.owner.focus_id == drop.focus_id
            ):
                self._start_reflow(drop.start_nodes)
        return changed

    def cancel(self):
        """幂等取消未提交手势；提交后动画只收束到终点，不回滚文档或折叠状态。"""
        self._settle_reflow()
        self._scroll_timer.stop()
        if self._graph_drag is not None:
            _, _, base, _ = self._graph_drag
            self._graph_drag = None
            if not self.owner._disposed:
                self.owner._show_edit_layout(base)
            return
        if not self.active:
            return
        layer, self.ghost = self.ghost, None
        self.owner.scene.removeItem(layer)
        self.target = None
        self._blocked_submap = None
        if not self.owner._disposed:
            # 拖动期间只改变源图元透明度，取消无需重建图元、重排或恢复折叠快照。
            for key in self.owner.node_items:
                if key in self._resolver.moving:
                    self.owner.node_items[key].setOpacity(1)
            for key in self.owner.branch_items:
                if key in self._resolver.moving:
                    self.owner.branch_items[key].setOpacity(1)
        self._display_nodes = {}
        self._base = self._source = self._resolver = None
        self._ghost_nodes = self._ghost_branches = None
        self._ghost_node_bounds = self._ghost_branch_bounds = None
        self._external_forbidden.clear()
        if self._external_mime is not None:
            self._external_mime.deleteLater()
            self._external_mime = None
