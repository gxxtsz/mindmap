"""逻辑选择与视口图元的投影；屏外节点不依赖 QGraphicsItem 存活。"""

from contextlib import contextmanager

from .items import NodeItem


class NodeSelectionMixin:
    """仅在 GUI 线程维护选择；公开查询不创建图元、不访问引擎快照。"""

    def _init_selection(self):
        self._selected_ids = set()
        self._ordered_selection = ()
        self._notified_selection = ()
        self._projected_selection = set()
        self._selection_revision = 0
        self._selection_event_depth = 0
        self._selection_event_pending = False
        self._layout_selection_ids = None

    @property
    def selection_count(self) -> int:
        """返回当前页完整逻辑选择的数量，包含屏外节点，复杂度为 O(1)。

        只读属性；不生成列表、不排序、不实例化图元。正文编辑的文字选择不计入。
        """
        return len(self._selected_ids)

    def is_node_selected(self, node_id: str) -> bool:
        """以 O(1) 查询节点是否逻辑选中；未知、折叠隐藏或其他页面的标识返回 False。

        仅借用节点标识，不改变选择、视口或文档；屏外节点与屏内节点使用相同规则。
        """
        return node_id in self._selected_ids

    def _selection_in_order(self):
        if self._ordered_selection is None:
            if len(self._selected_ids) < 2:
                self._ordered_selection = tuple(self._selected_ids)
            else:
                if self._node_order is None:
                    self._node_order = {key: rank for rank, key in enumerate(self._document.ordered_nodes())}
                self._ordered_selection = tuple(sorted(self._selected_ids, key=self._node_order.__getitem__))
        return self._ordered_selection

    def _invalidate_selection_order(self):
        """仅在层级、同级次序或页面变化时失效；样式和文本变化保留树序缓存。"""
        self._node_order = None
        self._ordered_selection = None

    def selected_node_ids(self) -> list[str]:
        """按当前页树的先序返回完整选择的列表副本，包含尚未实例化的屏外节点。

        内部缓存有序结果；调用方修改返回列表不会影响画布。不展开折叠节点、不
        创建图元，排序只发生在选择或树结构改变后第一次查询时。
        """
        return list(self._selection_in_order())

    def current_node_id(self) -> str:
        """取得树序首个选中节点；无选择时回退到聚焦节点及当前页根节点。

        复用选择缓存，不复制完整列表，也不改变导航、折叠状态或撤销历史。
        """
        ids = self._selection_in_order()
        return ids[0] if ids else self.focus_id or self._document.root_id

    def _filter_selection(self, node_ids):
        # 大批量操作只遍历布局标识，避免 Mapping.__contains__ 为每个节点求世界矩形。
        ids = set(node_ids)
        if len(ids) > 32:
            if self._layout_selection_ids is None:
                self._layout_selection_ids = set(self.layout_result.nodes)
            return ids & self._layout_selection_ids
        return {key for key in ids if key in self.layout_result.nodes}

    def _store_selection(self, ids):
        if ids == self._selected_ids:
            return False
        self._selected_ids = ids
        self._ordered_selection = None
        self._selection_revision += 1
        return True

    def _project_selection(self):
        """只更新现存图元；回收或恢复屏外图元不能反向改变逻辑选择。"""
        blocked = self.scene.blockSignals(True)
        try:
            for key, item in self.node_items.items():
                item.setSelected(key in self._selected_ids)
            self._projected_selection = self._selected_ids.intersection(self.node_items)
        finally:
            self.scene.blockSignals(blocked)

    def select_nodes(self, node_ids) -> None:
        """用当前布局内的有效标识替换选择；折叠后代和聚焦之外的节点被过滤。

        输入只在调用期间借用，新增节点不会自动继承此前的全选。仅保留视口图元
        和树序首个操作节点，不为整批屏外节点创建图元。实际选择改变时同步发出
        一次 selection_changed(list)，重复相同选择不重复通知。仅在 GUI 线程调用。
        """
        self.view.cancel_interaction()
        self.view.graph_edge_edit.clear_selection()
        self._store_selection(self._filter_selection(node_ids))
        if self._virtualized:
            self._sync_viewport()
        else:
            self._project_selection()
        self._notify_selection()

    def select_all_nodes(self) -> None:
        """选择当前页已展开的全部布局节点，包含屏外节点；不跨页、不改变折叠状态。

        只读画布也可调用；正文编辑及已释放状态直接返回，由文本编辑器处理 Ctrl+A。
        复用批量选择入口，保持文档、撤销历史和视口位置不变，不实例化全部图元。
        """
        if self._disposed or self.editor is not None:
            return
        if self._layout_selection_ids is None:
            self._layout_selection_ids = set(self.layout_result.nodes)
        self.select_nodes(self._layout_selection_ids)

    @contextmanager
    def _scene_selection_event(self, *, replace=False):
        """合并一次 Qt 鼠标事件的中间信号；普通按下替换，Ctrl 和框选沿用增减量。"""
        self._selection_event_depth += 1
        try:
            yield
        finally:
            self._selection_event_depth -= 1
            if not self._selection_event_depth and (replace or self._selection_event_pending):
                self._selection_event_pending = False
                self._read_scene_selection(replace=replace)

    def _selection_changed(self):
        if self._refreshing or self._disposed:
            return
        if self._selection_event_depth:
            self._selection_event_pending = True
        else:
            # 保留宿主直接调用 scene.clearSelection() 的取消选择语义。
            self._read_scene_selection(clear_empty=True)

    def _read_scene_selection(self, *, replace=False, clear_empty=False):
        current = {item.node.id for item in self.scene.selectedItems() if isinstance(item, NodeItem)}
        if replace or (clear_empty and not current):
            ids = current
        else:
            ids = (self._selected_ids - self._projected_selection) | current
        self._projected_selection = current
        self._store_selection(ids)
        self._notify_selection()

    def _notify_selection(self):
        if self._refreshing or self._disposed:
            return
        ordered = self._selection_in_order()
        if ordered:
            self.view.graph_edge_edit.clear_selection()
        if ordered == self._notified_selection:
            return
        self._notified_selection = ordered
        if self._virtualized:
            self._sync_viewport()
        self.view.image_resize.cancel()
        self.view._update_selection_decoration()
        self.view.viewport().update()
        self._cancel_branch_menu()
        self._update_actions()
        self.view.update_cursor()
        self.selection_changed.emit(list(ordered))
