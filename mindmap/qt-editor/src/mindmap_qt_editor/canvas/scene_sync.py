"""将引擎的已提交变更同步到画布读模型、布局和视口图元。"""

from mindmap_core import ChangeEvent, ChangeSet, LayoutResult, Rect
from mindmap_core.layouts import create_incremental_layout, supports_incremental_layout
from mindmap_core.node_pages import parse_graph_page_id
from mindmap_qt_render.rendering import BranchItem
from PySide6.QtCore import QRectF

from .items import NodeItem

VIRTUALIZATION_NODE_LIMIT = 1000


class SceneSyncMixin:
    """由画布在 GUI 线程同步读模型和场景，复用画布持有的缓存与图元。

    正式变更先按引擎事件更新读模型，再刷新受影响的测量、布局和视口。
    场景与图元由画布拥有，调用方通过画布的刷新和查询接口使用结果。
    """

    def _apply_engine_event(self, event):
        """同步一条已提交事件，并在读模型切换前让宿主处理旧输入。

        保存事件只同步保存状态。结构和显示变更按现有选择及视口增量更新，
        文档替换重新建立读模型。此方法由画布的引擎订阅回调在 GUI 线程调用。
        """
        if self._disposed:
            return
        external = self.is_external_event(event)
        changes = event.changes
        if event.kind != "saved":
            history_graph = (parse_graph_page_id(changes.page_id)
                             if changes is not None and event.kind in ("undo", "redo") else None)
            if self.graph is not None or (history_graph is not None and not external):
                self._apply_graph_event(event)
                return
        if event.kind != "saved" and changes is not None:
            inspiration_topology = "inspiration" in changes.settings
            if self.current_page_kind == "inspiration" and not inspiration_topology:
                # 只在成员或页面关系可能改变时重建当前想法。跨页引用的正文变更
                # 与普通属性编辑一样，沿用节点差量，不重新测量整棵想法。
                inspiration_topology = (
                    any(changes.collections.get(name) for name in ("submaps", "detail_pages"))
                    or any(old is None or new is None or old.children != new.children
                           for old, new in changes.collections.get("nodes", {}).values())
                    or not external and event.kind in ("undo", "redo") and changes.page_id != self.current_page_id
                )
            if inspiration_topology:
                self._apply_inspiration_event(event)
                return
        # 在画布刷新引发选择信号之前通知宿主，防止旧侧栏输入被提交到新文档或撤销状态。
        if event.kind != "saved":
            self.view.image_resize.cancel()
            self.view.node_drag.document_changed()
            # 自动保存只更新保存标记，不触发文档替换通知，不打断菜单或未完成输入。
            self.document_changing.emit(event)
            self._cancel_branch_menu()
            self._cache_page_view()
            before = self.selected_node_ids()
            old_parents = {key: self._node_parents.get(key) for key in before}
            old_layout, old_theme = self._document.layout, self._document.theme
            if self.editor is not None:
                pair = (event.changes.collections.get("nodes", {}).get(self.editor.node_id)
                        if event.changes is not None else None)
                changed = pair[1 if event.forward else 0] if pair is not None else None
                invalid = pair is not None and (changed is None or changed.page_reference_root_id is not None)
                # 已删除或转为引用的节点不能继续编辑；reset 不查询旧实体的页归属。
                if (not external or event.changes is None or invalid
                        or self.engine.node_page_id(self.editor.node_id) != self.current_page_id):
                    self.finish_edit(commit=False)
            switched = False
            local_changes = None
            if changes is not None:
                index = 1 if event.forward else 0
                delta = changes.collections.get("nodes", {})
                # 页面边界变化可能迁移整棵分支，但随行后代自身没有字段差量。
                # 普通分支通过移动记录识别；复用的包装根没有普通移动记录，
                # 由其仍存在的实体识别。空页创建和级联删除没有存量成员迁移，
                # 继续走字段差量，避免大图里给叶子创建空子图也重排整个父页。
                topology = [page for old, new in changes.collections.get("submaps", {}).values()
                            if old is None or new is None or (old.root_id, old.owner_id) != (new.root_id, new.owner_id)
                            for page in (old, new) if page is not None]
                retained_roots = [page for page in topology if page.root_id not in delta
                                  or all(node is not None for node in delta[page.root_id])]
                reproject = any(move.before.page_id != move.after.page_id
                                and self.current_page_id in (move.before.page_id, move.after.page_id)
                                for move in changes.moves)
                reproject = reproject or any(
                    page.id == self.current_page_id or page.owner_id in self._document.nodes
                    or page.root_id in self._document.nodes for page in retained_roots)
                changes.apply(self._book_document, forward=event.forward)
                target_page = self._current_page_id
                if event.kind in ("undo", "redo") and not external:
                    target_page = changes.page_id
                if not self._book_document.has_page(target_page):
                    target_page = next((page for page in reversed(self._page_chain)
                                        if self._book_document.has_page(page)), None)
                switched = target_page != self._current_page_id
                if switched or reproject:
                    inspiration_root = None
                    if switched and target_page == self.engine.inspiration_id:
                        # 从收藏等入口直接打开想法的子图时，本视图记住的上一个
                        # 想法可能无关。页面消失后沿旧页面链的实际入口找所属根，
                        # 不能误回到另一棵想法；不存在时仍沿用常规空态回退。
                        changed_pages = {page.id: page for page in topology}
                        for page_id in reversed(self._page_chain):
                            page = changed_pages.get(page_id)
                            if page is not None:
                                inspiration_root = self._inspiration_root_for(page.owner_id)
                                if inspiration_root is not None:
                                    break
                    self._switch_page(target_page, notify=False, cache=False,
                                      inspiration_root_id=inspiration_root,
                                      detail_origin=self._detail_origin if not switched else None,
                                      reference_origin=self._reference_origin if not switched else None)
                    if switched:
                        before = self.selected_node_ids()
                        old_parents = {key: self._node_parents.get(key) for key in before}
                # 全局差量只合并属于当前页的节点；其他页的新增节点不能漏进画布或大纲。
                local_delta = {key: pair for key, pair in delta.items()
                               if key in self._document.nodes or (pair[index] is not None
                                   and self.current_page_id != self.engine.inspiration_id
                                   and self.engine.node_page_id(key) == self._current_page_id)}
                # 目标页改正文时，原始差量不会改写引用节点。用反查索引补齐同页显示
                # 差量，从画布旧投影和最新完整读模型取两端，供测量与大纲共用。
                display_ids = dict.fromkeys(local_delta)
                if not switched and not reproject:
                    for key, (old, new) in delta.items():
                        if (old is not None and new is not None
                                and (old.text, old.text_format) != (new.text, new.text_format)):
                            for source_id in self.engine.page_reference_sources(key):
                                if source_id in self._document.nodes:
                                    display_ids[source_id] = None
                delta = local_delta
                new_ids = [key for key, pair in delta.items()
                           if pair[1 - index] is None and pair[index] is not None]
                for key, pair in delta.items():
                    old = pair[1 - index]
                    new = pair[index]
                    if old is not None and (new is None or old.children != new.children):
                        for child in old.children:
                            if self._node_parents.get(child) == key:
                                self._node_parents.pop(child)
                    if pair[index] is None:
                        self._node_parents.pop(key, None)
                display_delta = {}
                for key in display_ids:
                    old = self._document.nodes.get(key)
                    new = self._book_document.display_node(key) if key in self._book_document.nodes else None
                    if key in local_delta or old != new:
                        display_delta[key] = (old, new) if event.forward else (new, old)
                    if new is None:
                        self._document.nodes.pop(key, None)
                    else:
                        self._document.nodes[key] = new
                source = self._book_document.page_record(self._current_page_id)
                self._document.layout, self._document.theme = source.layout, source.theme
                self._document.assets = self._book_document.assets
                self._document.metadata = self._book_document.metadata
                for key, pair in delta.items():
                    node = pair[index]
                    old = pair[1 - index]
                    if node is not None and (old is None or old.children != node.children):
                        for child in node.children:
                            self._node_parents[child] = key
                self._measure_dirty = (None if old_theme != source.theme or switched or reproject
                                       else tuple(display_delta))
                if not switched and not reproject:
                    settings = {}
                    for name, old, new in (("layout", old_layout, source.layout),
                                           ("theme", old_theme, source.theme)):
                        if old != new:
                            settings[name] = (old, new) if event.forward else (new, old)
                    local_changes = ChangeSet(
                        {"nodes": display_delta, "assets": changes.collections.get("assets", {})},
                        settings, self._current_page_id)
            else:
                self._book_document = self._read_engine_document()
                self._current_page_id = None
                self._inspiration_root_id = self._last_inspiration_root = None
                self._page_states.clear()
                self._detail_origin = None
                self._reference_origin = None
                self._page_chain = [None]
                self._view_restore_center = None
                self._view_reset_pending = False
                self._view_reset_timer.stop()
                self.view._hidden_center = None
                self._document = self._book_document.page_view()
                self._node_parents = self._document.parents()
                new_ids = []
                self._measure_dirty = None
                self._incremental_layout = None
            if local_changes is None or any(
                old is None or new is None or old.children != new.children
                for old, new in local_changes.collections.get("nodes", {}).values()
            ) or any(key in local_changes.settings for key in ("root_id", "inspiration")):
                self._invalidate_selection_order()
            after = [key for key in before if key in self._document.nodes]
            parents = self._node_parents
            if new_ids and not external:
                new_set = set(new_ids)
                after = [
                    key for key in new_ids if parents.get(key) not in new_set
                ]
            if not after and (before or not external):
                after = [old_parents[key] for key in before if old_parents.get(key) in self._document.nodes]
                after = after or [self._document.root_id]
            if event.kind == "reset":
                self.focus_id = None
                self._selection_undo.clear()
                self._selection_redo.clear()
                after = [self._document.root_id]
            elif event.kind == "undo" and self._selection_undo:
                selection = self._selection_undo.pop()
                self._selection_redo.append(selection)
                if not external:
                    after = selection[0]
            elif event.kind == "redo" and self._selection_redo:
                selection = self._selection_redo.pop()
                self._selection_undo.append(selection)
                if not external:
                    after = selection[1]
            elif event.kind == "change":
                self._selection_undo.append((before, after))
                del self._selection_undo[: -self.engine.history_limit]
                self._selection_redo.clear()
            if self.focus_id not in self._document.nodes:
                self.focus_id = None
            self.refresh()
            if self.editor is not None:
                self.editor.base_layout = self.layout_result
                self._update_edit_preview(self.editor, force=True)
            after = [key for key in after if key in self._document.nodes]
            self.select_nodes(after)
            event = ChangeEvent(event.operation, tuple(local_changes.node_ids) if local_changes else (),
                                event.revision, event.kind, local_changes, event.forward, event.source_id)
        self._update_actions()
        self.history_changed.emit(self.engine.can_undo, self.engine.can_redo, self.engine.is_dirty)
        self.document_changed.emit(event)

    def _apply_graph_event(self, event):
        """按所属树节点更新图投影，树节点 ID 和图内局部 ID 始终分别解析。

        本地撤销和重做可以进入受影响的图。其他标签只更新内容，不抢占视图；当前图
        或其入口被删除时，优先返回仍存在的所属节点或祖先页。纯保存走原有通知路径。
        """
        self.view.node_drag.document_changed()
        self.document_changing.emit(event)
        self._cache_page_view()
        page = self.current_page_id
        owner = parse_graph_page_id(page)
        previous = self.graph
        before = self.selected_node_ids()
        changes = event.changes
        external = self.is_external_event(event)
        was_empty = previous is not None and not previous.nodes
        if changes is None:
            self.finish_edit(commit=False)
            self._book_document = self._read_engine_document()
            self._page_states.clear()
            self._selection_undo.clear()
            self._selection_redo.clear()
            self._switch_page(None, notify=False, cache=False)
        else:
            changes.apply(self._book_document, forward=event.forward)
            destination = changes.page_id if event.kind in ("undo", "redo") and not external else page
            destination_owner = parse_graph_page_id(destination)
            return_owner = None
            if not self._book_document.has_page(destination):
                return_owner = destination_owner or owner
                if return_owner in self._book_document.nodes:
                    destination = self.engine.node_page_id(return_owner)
                else:
                    return_owner = None
                    destination = next((item for item in reversed(self._page_chain)
                                        if self._book_document.has_page(item)), None)
            elif destination != page and owner in self._book_document.nodes:
                if self.engine.node_page_id(owner) == destination:
                    return_owner = owner
            if destination == page and owner is not None:
                updated = self._book_document.page_view(page)
                self._refresh_graph_projection(updated, external)
                if was_empty and updated.nodes:
                    # 空图初次获得内容后只移动视口中心，保留用户已调整的缩放倍率。
                    self.center_root()
            else:
                self.finish_edit(commit=False)
                self._switch_page(destination, notify=False, cache=False,
                                  inspiration_root_id=self._inspiration_root_for(return_owner))
                if return_owner in self._document.nodes and not self.is_node_selected(return_owner):
                    self._locate_page_node(return_owner)

            after = self.selected_node_ids()
            if event.kind == "change":
                if (not external and self.current_page_id == page and previous is not None
                        and self.graph is not None):
                    old_ids = {node.id for node in previous.nodes}
                    fresh = [node.id for node in self.graph.nodes if node.id not in old_ids]
                    if fresh:
                        after = fresh
                        self.select_nodes(after)
                self._selection_undo.append((before, after))
                del self._selection_undo[:-self.engine.history_limit]
                self._selection_redo.clear()
            elif event.kind in ("undo", "redo"):
                undoing = event.kind == "undo"
                source, target = ((self._selection_undo, self._selection_redo) if undoing else
                                  (self._selection_redo, self._selection_undo))
                if source:
                    snapshot = source.pop()
                    target.append(snapshot)
                    if not external and self.current_page_id == page and self.graph is not None:
                        self.select_nodes(snapshot[0 if undoing else 1])
        self._update_actions()
        self.history_changed.emit(self.engine.can_undo, self.engine.can_redo, self.engine.is_dirty)
        # 图的节点是页内投影，所属树节点的差量不能交给图的大纲当作图节点差量。
        self.document_changed.emit(ChangeEvent(
            event.operation, (), event.revision, event.kind, None, event.forward, event.source_id))

    def _refresh_graph_projection(self, updated, external):
        """在同一页回显最新提交，保留未被外部改写的原地草稿及原视口。"""
        previous = self._document
        if self.editor is not None:
            key = self.editor.node_id
            if key not in updated.nodes or (not external and previous.nodes.get(key) != updated.nodes[key]):
                self.finish_edit(commit=False)
        if previous.graph == updated.graph and previous.theme == updated.theme:
            self._document.title = updated.title
            return
        self._document = updated
        self._invalidate_selection_order()
        if previous.theme != updated.theme:
            self._measure_dirty = None
        else:
            self._measure_dirty = tuple(key for key in previous.nodes.keys() | updated.nodes.keys()
                                        if previous.nodes.get(key) != updated.nodes.get(key))
        if not updated.graph.hidden_node_ids or not updated.graph.edges:
            self._graph_show_all = False
        self.refresh()
        if self.editor is not None:
            self.editor.base_layout = self.layout_result
            self._update_edit_preview(self.editor, force=True)
        self._notify_selection()

    def _detail_measurement_changes(self, document, dirty):
        """把页外共享详情变化折算为当前页的测量差量，不改借用节点的归属字段。

        完整刷新时建立根节点和子导图入口的候选表；普通编辑只维护变化候选。
        仅在附属页面关系变化时复查候选，避免每次输入都扫描当前页或整份文件。
        目标变化的别名加入返回的脏节点，保证增量布局使用新的图标占位宽度。
        dirty 为 None 时保持完整测量语义；返回值仅供当前 GUI 刷新使用。
        """
        roots = document.root_ids
        if dirty is None:
            self._detail_targets.clear()
            candidates = {key for key, node in document.nodes.items() if key in roots or node.submap_id}
        else:
            candidates = set(self._detail_targets) if self._detail_targets_dirty else set()
            for key in dirty:
                node = document.nodes.get(key)
                if node is not None and (key in roots or node.submap_id):
                    candidates.add(key)
                else:
                    self._detail_targets.pop(key, None)
                    candidates.discard(key)
        changed = []
        for key in candidates:
            if key not in document.nodes:
                self._detail_targets.pop(key, None)
                continue
            target = self.engine.resolve_detail_page(key)
            if self._detail_targets.get(key) != target:
                changed.append(key)
            self._detail_targets[key] = target
        self._detail_targets_dirty = False
        return None if dirty is None else tuple(dict.fromkeys((*dirty, *changed)))

    def refresh(self) -> None:
        """用提交事件维护的界面读模型刷新几何，复用按 ID 索引的图元。

        局部提交只合并变化实体；只有文档替换才重建完整读模型。这里不能再次读取
        engine.document，以免恢复整图深拷贝。内置树布局复用持久化摘要和局部路径，
        大图仅同步视口图元；聚焦和第三方布局继续使用兼容路径。
        """
        self.view.cancel_interaction()
        self._cancel_branch_menu(restore=False)
        # 在更新场景前记录中心，内容扩展时保持视图位置，删除或折叠导致范围缩小
        # 时再由视图限制到最近边界；不要为了容纳内容而自动修改用户的缩放比例。
        center = self.view.mapToScene(self.view.viewport().rect()).boundingRect().center()
        document = self._document
        dirty = (self._measure_dirty if self.graph is not None
                 else self._detail_measurement_changes(document, self._measure_dirty))
        resolver = self.engine.resolve_detail_page if self.graph is None else None
        if dirty is None or self._visual_document is None:
            visuals, sizes = self.factory.measure(document, detail_page_resolver=resolver)
        else:
            visuals, sizes = self._visuals, self._measurements
            self.factory.update_measurements(
                document, dirty, visuals, sizes, detail_page_resolver=resolver)
        self._visual_document, self._visuals = document, visuals
        self._measure_dirty = ()
        self._measurements = sizes
        if self.graph is not None:
            self._incremental_layout = None
            self.layout_result = self._calculate_graph_layout(sizes)
        elif self._can_incremental():
            key = (id(self.layout_registry.get(document.layout.algorithm_id)),
                   document.root_id, repr(document.layout))
            if self._incremental_layout is None or key != self._incremental_key or dirty is None:
                self._incremental_layout = create_incremental_layout(self.layout_registry, document, sizes)
                self._incremental_key = key
            else:
                self._incremental_layout.update(document, sizes, dirty)
            self.layout_result = self._incremental_layout.result()
        else:
            self._incremental_layout = None
            self.layout_result = self.layout_registry.layout(document, sizes, focus_id=self.focus_id)
        # 完整布局只推导一次方向；增量路径直接复用几何索引，避免每个按钮遍历整图。
        self._fold_sides = (
            document.branch_sides() if self.graph is None and self._incremental_layout is None
            and document.layout.direction == "both"
            else {}
        )
        result = self.layout_result
        self._layout_selection_ids = None
        self._store_selection(self._filter_selection(self._selected_ids))
        self._virtualized = self._incremental_layout is not None and len(result.nodes) >= VIRTUALIZATION_NODE_LIMIT
        self._viewport_signature = None
        self._refreshing = True
        self.scene.blockSignals(True)
        try:
            bounds = result.bounds
            # 布局边界已覆盖可见节点、节点内图片及普通父子分支线。场景
            # 只记录内容范围，一屏操作余量由视图按当前窗口大小与倍率单独计算。
            self.scene.setSceneRect(QRectF(bounds.x, bounds.y, bounds.width, bounds.height))
            self.view.update_navigation_rect(center=center)
            if self._virtualized:
                self._sync_viewport(force=True)
            else:
                self._set_node_items(document, visuals, result)
                self._set_branch_items(result)
            self._apply_ui_theme()
        finally:
            self.scene.blockSignals(False)
            self._refreshing = False
        self.view.update_cursor()
        self._schedule_view_reset()
        if self.editor is not None:
            self.editor.sync_geometry(force=True)

    def _can_incremental(self):
        """由布局提供者判断优化能力；聚焦仍沿用完整布局，不绕过宿主注入入口。"""
        return (self.graph is None and self.focus_id is None
                and self.current_page_kind != "inspiration"
                and supports_incremental_layout(self.layout_registry, self._document.layout.algorithm_id))

    def _fold_direction(self, node_id: str) -> str:
        """返回按钮伸出的方向；双向根节点固定向右，其他节点跟随所属一级分支。

        普通三种方向直接读取配置。双向增量画布沿用几何的祖先索引查询，聚焦或
        自定义布局复用 refresh 中一次生成的方向映射；悬停不重建方向或布局。
        """
        direction = self._document.layout.direction
        if direction != "both":
            return direction
        if node_id == self._document.root_id:
            return "right"
        if self._incremental_layout is not None:
            return self._incremental_layout.geometry.side(node_id)
        return self._fold_sides.get(node_id, "right")

    def _ensure_node_item(self, key):
        """为程序选择或编辑按需实例化一个可见节点，不改变文档折叠或视口位置。"""
        if key in self.node_items or not hasattr(self, "layout_result"):
            return
        rect = self.layout_result.nodes.get(key)
        if rect is None:
            return
        item = NodeItem(self._document.nodes[key], self._visuals[key], self._document.theme, self)
        self.scene.addItem(item)
        self.node_items[key] = item
        item.setPos(rect.x, rect.y)
        blocked = self.scene.blockSignals(True)
        try:
            item.setSelected(self.is_node_selected(key))
            if item.isSelected():
                self._projected_selection.add(key)
        finally:
            self.scene.blockSignals(blocked)

    def _viewport_changed(self):
        """平移、缩放或尺寸变化后同步屏内对象，再通知宿主记录视图现场。"""
        if (getattr(self, "_refreshing", True) or self._disposed or self.view._viewport_sync_depth):
            return
        if self._virtualized:
            self._sync_viewport()
        self.viewport_changed.emit()

    def _sync_viewport(self, *, force=False):
        """保留首个操作节点、编辑和鼠标捕获对象，其余图元仅维护视口及预取边距。

        连接线采用父级局部路径和世界原点。子树平移时只移动现有图元，保持 Qt
        路径和画笔不变；视口外实体仍存在于独立读模型和按需布局映射中。
        """
        selected = {self.current_node_id()} if self.selection_count else set()
        if self.editor is not None:
            selected.add(self.editor.node_id)
        grabbed = self.scene.mouseGrabberItem()
        if isinstance(grabbed, NodeItem):
            selected.add(grabbed.node.id)
        if self.view.press_node is not None:
            selected.add(self.view.press_node)
        box = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        signature = (id(self.layout_result), box.x(), box.y(), box.width(), box.height(),
                     frozenset(selected), self._selection_revision)
        if not force and signature == self._viewport_signature:
            return
        self._viewport_signature = signature
        margin = 100 / self.view.transform().m11()
        viewport = Rect(box.x(), box.y(), box.width(), box.height()).padded(margin)
        nodes, branches = self.layout_result.viewport(viewport, selected)
        blocked = self.scene.blockSignals(True)
        try:
            self._set_node_items(self._document, self._visuals, LayoutResult(nodes=nodes))
            for key in list(self.branch_items):
                if key not in branches:
                    self.scene.removeItem(self.branch_items.pop(key))
            for key, (branch, origin) in branches.items():
                item = self.branch_items.get(key)
                if item is None:
                    item = self.branch_items[key] = BranchItem(branch)
                    self.scene.addItem(item)
                else:
                    item.set_branch(branch)
                item.setPos(*origin)
            # 同色主干去重的所有权和跨颜色透明叠加依赖稳定绘制次序。对象复用时
            # 不能沿用创建先后顺序，否则平移到另一视口后交汇处颜色会改变。
            ordered = sorted(branches, key=self.layout_result.geometry.layout_order)
            for previous, following in zip(ordered, ordered[1:]):
                self.branch_items[previous].stackBefore(self.branch_items[following])
            if self.view.node_drag.active:
                self.view.node_drag._dim_sources()
        finally:
            self.scene.blockSignals(blocked)

    def _set_node_items(self, document, visuals, result) -> None:
        """按给定快照展示节点，不改写正式文档和布局结果。

        调用方在批量更新期间屏蔽场景选择信号；复用现有图元以保留选择和鼠标捕获。
        相同快照节点和视觉跳过几何重建，动画位置由拖动控制器在正式刷新后设置。
        拖动期间不调用此方法，折叠主题仅在放下后的正式刷新中展开。
        """
        for key in list(self.node_items):
            if key not in result.nodes:
                self.scene.removeItem(self.node_items.pop(key))
        for key, rect in result.nodes.items():
            # 原地编辑只覆盖当前节点的展示视觉；正式尺寸和共享缓存仍保留最后
            # 一次提交的数据。虚拟化在滚动时重用图元，也必须保留这份草稿外观。
            visual = (self.editor.preview_visual
                      if self.editor is not None and key == self.editor.node_id else visuals[key])
            item = self.node_items.get(key)
            controls_changed = (item is None or item.visual is not visual
                                or item.node is not document.nodes[key] or item.theme is not document.theme
                                or getattr(item, "_controls_layout", None) is not self.layout_result)
            if item is None:
                item = NodeItem(document.nodes[key], visual, document.theme, self)
                self.scene.addItem(item)
                self.node_items[key] = item
            elif (
                item.visual is not visual
                or item.node.expanded != document.nodes[key].expanded
                or item.node.note != document.nodes[key].note
                or item.node.hyperlink != document.nodes[key].hyperlink
                or item.theme.accent != document.theme.accent
            ):
                item.update_content(document.nodes[key], visual, document.theme)
            else:
                # 视觉未变也同步实体引用，正文导航等读取当前快照，但不使 Qt 几何失效。
                item.node, item.theme = document.nodes[key], document.theme
            item.setSelected(self.is_node_selected(key))
            # 平移和缩放不改变按钮的场景几何；只在实体、尺寸、主题或布局变化时
            # 重建控件路径。选中、悬停产生的可见性变化仍由各自事件即时同步。
            if controls_changed:
                item.sync_fold_control()
                item._controls_layout = self.layout_result
            item.setPos(rect.x, rect.y)
        self._projected_selection = self._selected_ids.intersection(self.node_items)
        self.view._update_selection_decoration()

    def _set_branch_items(self, result) -> None:
        """只更新分支图元，不触碰节点、编辑器或选择，预览与提交共用此入口。"""
        for key in list(self.branch_items):
            if key not in result.branches:
                self.scene.removeItem(self.branch_items.pop(key))
        for key, branch in result.branches.items():
            if key not in self.branch_items:
                self.branch_items[key] = BranchItem(branch)
                self.scene.addItem(self.branch_items[key])
            else:
                self.branch_items[key].set_branch(branch)
            self.branch_items[key].setPos(0, 0)
        self.view.graph_edge_edit.sync_visibility()
