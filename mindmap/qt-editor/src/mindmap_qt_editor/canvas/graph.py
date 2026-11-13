"""图页的读模型、编辑命令和临时预览，操作都落在当前思维导图画布上。"""

from collections.abc import Iterable
from dataclasses import replace

from mindmap_core import GraphEdge, GraphNode, MindMapError, NodeGraph
from mindmap_core.formats.graph import format_graph_text, parse_graph_text
from mindmap_core.layouts.graph import build_graph_layout, graph_layout
from mindmap_core.node_pages import GRAPH_PAGE_TYPE, graph_page_id, parse_graph_page_id
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QPainterPathStroker
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
)

from .interactions import document_interaction

_TREE_ACTIONS = (
    "properties", "branch_style", "insert_parent", "move_up", "move_down", "fold", "expand_all",
    "collapse_all", "image", "image_url", "remove_image", "hyperlink", "add_inspiration",
    "move_to_inspiration", "attach_inspirations", "group_inspirations", "create_submap",
    "convert_descendants_to_submap", "convert_submap_to_descendants", "enter_submap",
    "favorite_submap", "delete_submap", "create_detail_page", "enter_detail_page",
    "delete_detail_page", "set_page_reference", "open_page_reference", "clear_page_reference",
)
_GRAPH_ACTIONS = ("graph_text", "graph_add_edge", "graph_auto_layout", "graph_remove")


def _unused_node_ids(nodes, count):
    """按顺序分配易于阅读的局部标识，批量粘贴时一次避开已有标识。"""
    used = {node.id for node in nodes}
    keys = []
    index = 0
    while len(keys) < count:
        index += 1
        key = f"n{index}"
        if key not in used:
            keys.append(key)
    return keys


class _GraphTextEditor(QPlainTextEdit):
    """记录输入法候选，让显式应用按钮只提交用户已确认的文本。"""

    composing = False

    def inputMethodEvent(self, event):
        self.composing = bool(event.preeditString())
        super().inputMethodEvent(event)

    def source_text(self) -> str:
        """从 Qt 的各段落取原文，保留标题中的空格和分隔符，并恢复实际换行。"""
        lines = []
        block = self.document().firstBlock()
        while block.isValid():
            lines.append(block.text())
            block = block.next()
        return "\n".join(lines)


class GraphCanvasMixin:
    """图页只提供数据、几何和命令，节点图元、编辑器与视口归 MindMapWidget。"""

    @property
    def graph(self) -> NodeGraph | None:
        """只读借用活动图页的正式快照；树页返回 None，修改通过引擎命令提交。"""
        return getattr(self._document, "graph", None)

    @property
    def graph_edge_selection_count(self) -> int:
        """返回当前图页明确选中的连线数，供宿主更新状态栏。

        节点选择产生的高亮不计入。查询只借用本标签的选择集合，树页返回 0，
        不读取引擎快照、不更改选择或保存状态。仅在画布所属 GUI 线程读取。
        """
        return len(self.view.graph_edge_edit.selected_edges) if self.graph is not None else 0

    def _calculate_graph_layout(self, sizes):
        """拓扑和节点尺寸相同时复用分层结果，手动位置变化只更新显示路径。"""
        graph = self.graph
        color = self._document.theme.accent
        key = (tuple(sizes.items()), graph.edges, color)
        if self._graph_layout_cache is None or self._graph_layout_cache[0] != key:
            self._graph_layout_cache = (key, build_graph_layout(dict(sizes), graph.edges, color=color))
        return graph_layout(graph, self._graph_layout_cache[1])

    def _update_graph_actions(self, editing):
        """两种页面共用动作和快捷键，图页按局部选择与只读状态调整权限。"""
        graph = self.graph
        in_graph = graph is not None
        actions = self._actions
        # 无需改写树动作的可见性。图菜单只取图页支持的动作，禁用树动作以停用快捷键。
        actions["add_child"].setText("添加节点" if in_graph else "添加子节点")
        actions["add_sibling"].setText("添加节点" if in_graph else "添加同级节点")
        can_edit = self.editable and not editing
        for key in _GRAPH_ACTIONS:
            actions[key].setVisible(in_graph)
            actions[key].setEnabled(in_graph and not editing and (
                key == "graph_text" or can_edit and (key == "graph_remove" or bool(graph.nodes))))
        action = actions["node_graph"]
        node = self._document.nodes.get(self.current_node_id())
        eligible = not in_graph and self.selection_count == 1 and node is not None and not node.is_image_node
        existing = eligible and GRAPH_PAGE_TYPE in node.extension_pages
        action.setText("打开图" if existing else "添加图")
        action.setVisible(not in_graph and not (node and node.is_image_node))
        action.setEnabled(bool(eligible and not editing and (existing or self.editable)))
        if not in_graph:
            return
        for key in _TREE_ACTIONS:
            actions[key].setEnabled(False)
        for key in ("edit", "cut", "copy"):
            actions[key].setEnabled(bool(self.selection_count and not editing and (key == "copy" or self.editable)))
        actions["delete"].setEnabled(bool(can_edit and (self.selection_count or self.graph_edge_selection_count)))

    def _graph_context_menu_groups(self, on_canvas):
        """节点和空白处共用新增、图文本和自动布局入口。"""
        add_node = "add_sibling" if on_canvas else "add_child"
        groups = [("", (add_node, "graph_add_edge", "graph_text", "graph_auto_layout"))]
        if not on_canvas:
            groups.append(("", ("edit", "cut", "copy", "paste", "delete")))
        else:
            groups.append(("", ("paste", "fit", "center", "reset_zoom", "zoom_in", "zoom_out")))
        return [*groups, ("", ("undo", "redo", "graph_remove"))]

    @document_interaction
    def open_node_graph(self, node_id: str | None = None, *, create=False) -> None:
        """通过所属节点打开图页；显式创建时检查文字节点、只读权限及输入法状态。"""
        if self.graph is not None and node_id is None:
            return
        if node_id is None and self.selection_count != 1:
            raise MindMapError("请先选择一个文字节点")
        owner = self.current_node_id() if node_id is None else node_id
        self._prepare_page_navigation()
        if not self._book_document.has_page(graph_page_id(owner)):
            if not create:
                raise MindMapError("该节点没有图")
            self._ensure_editable()
            self.engine.set_node_graph(owner, NodeGraph())
        self._enter_graph_page(owner)

    def _enter_graph_page(self, owner: str) -> None:
        """输入检查已完成后切换图页，源树的视口和入口选择由共用导航记录。"""
        if owner in self._document.nodes and self.graph is None:
            self.select_nodes([owner])
        self._switch_page(graph_page_id(owner))

    def _graph_target(self) -> tuple[int, str]:
        """读取图的身份和最新已提交数据，防止模态对话框返回后覆盖另一个页面。"""
        owner = parse_graph_page_id(self.current_page_id)
        if self._disposed or owner is None or self.graph is None:
            raise MindMapError("当前页面没有图")
        return self.engine.document_generation, owner

    def _read_active_graph(self, target=None) -> NodeGraph:
        actual = self._graph_target()
        if target is not None and actual != target:
            raise MindMapError("图页已切换，请重新操作")
        graph = self.engine.node_graph(actual[1])
        if graph is None:
            raise MindMapError("图已删除")
        return graph

    def _write_active_graph(self, graph: NodeGraph, target=None) -> None:
        self._ensure_editable()
        actual = self._graph_target()
        if target is not None and actual != target:
            raise MindMapError("图页已切换，请重新操作")
        if self.engine.node_graph(actual[1]) is None:
            raise MindMapError("图已删除")
        self.engine.set_node_graph(actual[1], graph)

    @document_interaction
    def auto_layout_graph(self) -> None:
        """让当前节点图恢复内置 DOT 分层布局，并将发起操作的视图调整为适应窗口。

        在画布的 GUI 线程调用。当前页须为非空、可编辑的图，且没有正在编辑的
        节点正文或面板输入法候选，否则抛出 MindMapError。取消拖动预览后读取
        正式图，一次事务清空手动坐标，保留节点和连线。其他标签随事务同步，
        撤销可恢复全部原坐标。布局使用实测节点尺寸及完整连线，后续编辑会继续重排。
        已处于自动布局时只适应窗口，不写入历史。调用返回 None。
        """
        self._ensure_editable()
        target = self._graph_target()
        if self.is_editing or not all(guard() for guard in tuple(self._navigation_guards)):
            raise MindMapError("请先完成编辑，再自动布局")
        self.view.cancel_interaction()
        graph = self._read_active_graph(target)
        if not graph.nodes:
            raise MindMapError("请先添加图节点")
        if graph.positions:
            self._write_active_graph(replace(graph, positions={}), target)
        self.fit_to_view()

    @document_interaction
    def _commit_graph_edge(
        self, replacement: GraphEdge, *, original: GraphEdge | None = None,
        target: tuple[int, str] | None = None, expected: NodeGraph | None = None,
    ) -> bool:
        """新增或改接一条有向边，original 为空表示新增。

        对话框用 target 核对图页身份；拖拽还传入按下时的 expected 快照，
        避免在释放后覆盖其他视图的修改。校验完成后保留边的声明顺序和手动坐标，
        通过一次引擎事务提交。无变化或拖拽快照失效返回 False，其他校验失败交给调用方提示。
        """
        self._ensure_editable()
        if self.is_composing or not all(guard() for guard in tuple(self._navigation_guards)):
            raise MindMapError("请先完成输入法候选，再编辑连线")
        current = self._read_active_graph(target)
        if expected is not None and current != expected:
            return False
        if original is not None and original not in current.edges:
            raise MindMapError("这条连线已删除，请重新操作")
        if replacement == original:
            return False
        ids = {node.id for node in current.nodes}
        if replacement.source not in ids or replacement.target not in ids:
            raise MindMapError("起点或终点已删除，请重新操作")
        if replacement in current.edges:
            raise MindMapError("这条连线已存在")
        edges = list(current.edges)
        if original is None:
            edges.append(replacement)
        else:
            edges[edges.index(original)] = replacement
        self._write_active_graph(replace(current, edges=tuple(edges)), target)
        return True

    @document_interaction
    def _delete_graph_edges(self, edges: Iterable[GraphEdge], *, target: tuple[int, str] | None = None) -> None:
        """删除调用方明确指定的一组有向边，整组共享一次提交和一次撤销。

        edges 只在本次调用中读取，重复项合并，空集合不写入。菜单传入打开时的
        target（文档代次和所属节点），以免在切页或替换文件后删除同名边。提交前
        核对只读、文字编辑和输入法状态，并校验待删边仍全部存在。校验失败抛出
        MindMapError，其他节点、手动坐标和剩余连线的顺序保持原值。
        """
        self._ensure_editable()
        if self.is_editing or not all(guard() for guard in tuple(self._navigation_guards)):
            raise MindMapError("请先完成编辑，再删除连线")
        removed = frozenset(edges)
        if not removed:
            return
        current = self._read_active_graph(target)
        if not removed.issubset(current.edges):
            raise MindMapError("所选连线已变化，请重新选择")
        remaining = tuple(edge for edge in current.edges if edge not in removed)
        self._write_active_graph(replace(current, edges=remaining), target)

    @document_interaction
    def add_graph_node(self, *, connect_from_selection=False) -> str:
        """新增图节点并使用原地编辑器。Tab 可从单选节点接一条出边，Enter 新增独立节点。"""
        self._ensure_editable()
        self._prepare_page_navigation()
        graph = self._read_active_graph()
        key = _unused_node_ids(graph.nodes, 1)[0]
        nodes = (*graph.nodes, GraphNode(key, "新节点"))
        edges = graph.edges
        if connect_from_selection and self.selection_count == 1:
            edges = (*edges, GraphEdge(self.current_node_id(), key))
        self._write_active_graph(graph.with_content(nodes, edges))
        self.begin_edit(key)
        return key

    def _rename_graph_node(self, node_id, text):
        graph = self._read_active_graph()
        if node_id not in self._document.nodes:
            raise MindMapError("要编辑的图节点已删除")
        nodes = tuple(replace(node, title=text) if node.id == node_id else node for node in graph.nodes)
        self._write_active_graph(replace(graph, nodes=nodes))

    def _delete_graph_nodes(self, ids):
        """一次移除所选节点、相连的入边和出边以及失效的坐标。"""
        graph = self._read_active_graph()
        ids = set(ids)
        nodes = tuple(node for node in graph.nodes if node.id not in ids)
        edges = tuple(edge for edge in graph.edges if edge.source not in ids and edge.target not in ids)
        self._write_active_graph(graph.with_content(nodes, edges))

    def _copy_graph_nodes(self):
        ids = set(self.selected_node_ids())
        graph = self.graph
        if ids:
            fragment = NodeGraph(tuple(node for node in graph.nodes if node.id in ids),
                                 tuple(edge for edge in graph.edges if edge.source in ids and edge.target in ids))
            QApplication.clipboard().setText(format_graph_text(fragment))

    def _paste_graph_nodes(self) -> list[str]:
        self._ensure_editable()
        fragment = parse_graph_text(QApplication.clipboard().text())
        if not fragment.nodes:
            return []
        self._prepare_page_navigation()
        graph = self._read_active_graph()
        keys = _unused_node_ids((*graph.nodes, *fragment.nodes), len(fragment.nodes))
        remap = {node.id: key for node, key in zip(fragment.nodes, keys, strict=True)}
        nodes = (*graph.nodes, *(GraphNode(remap[node.id], node.title) for node in fragment.nodes))
        edges = (*graph.edges, *(GraphEdge(remap[edge.source], remap[edge.target]) for edge in fragment.edges))
        self._write_active_graph(graph.with_content(nodes, edges))
        self.reveal_nodes(keys)
        return keys

    @document_interaction
    def apply_graph_text(self, text: str) -> None:
        """整体替换图文本定义，先解析全文再提交，已有标识沿用手动坐标。"""
        self._ensure_editable()
        parsed = parse_graph_text(text)
        self._prepare_page_navigation()
        graph = self._read_active_graph()
        self._write_active_graph(graph.with_content(parsed.nodes, parsed.edges))

    def edit_graph_text(self) -> None:
        """打开本图的文本草稿。解析、权限或并发检查失败时在对话框内保留原文。"""
        self._prepare_page_navigation()
        target = self._graph_target()
        original = self._read_active_graph()
        dialog = QDialog(self)
        dialog.setWindowTitle("编辑图文本" if self.editable else "查看图文本")
        dialog.resize(640, 440)
        layout = QVBoxLayout(dialog)
        hint = QLabel('用 节点 app "应用" 声明节点，用 app -> core 连接两个已声明的节点。', dialog)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        editor = _GraphTextEditor(dialog)
        editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        editor.setObjectName("graph_text_editor")
        editor.setAccessibleName("图文本")
        editor.setReadOnly(not self.editable)
        editor.setPlainText(format_graph_text(original))
        layout.addWidget(editor, 1)
        error = QLabel(dialog)
        error.setObjectName("graph_text_error")
        error.setTextFormat(Qt.TextFormat.PlainText)
        error.setStyleSheet(f"color:{self.ui_colors.error};")
        error.setWordWrap(True)
        error.hide()
        layout.addWidget(error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, parent=dialog)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消" if self.editable else "关闭")
        buttons.rejected.connect(dialog.reject)
        if self.editable:
            apply = buttons.addButton("应用", QDialogButtonBox.ButtonRole.AcceptRole)
            apply.setAutoDefault(False)

            def submit():
                try:
                    if editor.composing:
                        raise MindMapError("请先完成输入法候选")
                    graph = self._read_active_graph(target)
                    if (graph.nodes, graph.edges) != (original.nodes, original.edges):
                        raise MindMapError("图内容已在其他视图更新，请复制草稿后重新打开")
                    self.apply_graph_text(editor.source_text())
                except (MindMapError, OSError) as exc:
                    error.setText(str(exc))
                    error.show()
                    editor.setFocus()
                    return
                dialog.accept()

            buttons.accepted.connect(submit)
        layout.addWidget(buttons)
        editor.setFocus()
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def choose_graph_edge(self) -> None:
        """从稳定节点标识选择起点和终点，确认时复查两端仍属于当前图。"""
        self._ensure_editable()
        self._prepare_page_navigation()
        target = self._graph_target()
        graph = self._read_active_graph()
        if not graph.nodes:
            raise MindMapError("请先添加图节点")
        dialog = QDialog(self)
        dialog.setWindowTitle("添加连线")
        layout = QFormLayout(dialog)
        source, destination = QComboBox(dialog), QComboBox(dialog)
        source.setAccessibleName("起点")
        destination.setAccessibleName("终点")
        for node in graph.nodes:
            label = " ".join(node.title.splitlines()) or "（空标题）"
            for field in (source, destination):
                field.addItem(f"{label}（{node.id}）", node.id)
        selected = self.selected_node_ids()
        first = selected[0] if selected else graph.nodes[0].id
        second = selected[1] if len(selected) > 1 else next(
            (node.id for node in graph.nodes if node.id != first), first)
        source.setCurrentIndex(source.findData(first))
        destination.setCurrentIndex(destination.findData(second))
        layout.addRow("起点", source)
        layout.addRow("终点", destination)
        error = QLabel(dialog)
        error.setTextFormat(Qt.TextFormat.PlainText)
        error.setStyleSheet(f"color:{self.ui_colors.error};")
        error.setWordWrap(True)
        error.hide()
        layout.addRow(error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
                                   parent=dialog)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("添加")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")

        def submit():
            try:
                edge = GraphEdge(source.currentData(), destination.currentData())
                self._commit_graph_edge(edge, target=target)
            except MindMapError as exc:
                error.setText(str(exc))
                error.show()
                return
            dialog.accept()

        buttons.accepted.connect(submit)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def graph_edge_at(self, position: QPoint) -> GraphEdge | None:
        """按视口逻辑像素查询点中的连线，空白、树页或正在编辑时返回 None。

        优先匹配实际的线身描边和箭头。线身附近保留固定屏幕容差，只有没有直接
        命中时才使用容差结果。同级命中按图中连线的绘制顺序取最上层。
        仅在所属 GUI 线程查询，不改写选线、视口或引擎状态。
        """
        if self.graph is None or self.is_editing:
            return None
        point = self.view.mapToScene(position)
        tolerance = 5 / self.zoom_factor
        stroker = QPainterPathStroker()
        stroker.setWidth(2 * tolerance)
        nearby = None
        for edge in reversed(self.graph.edges):
            item = self.branch_items.get(edge.key)
            if item is None:
                continue
            hit_bounds = item.sceneBoundingRect().adjusted(-tolerance, -tolerance, tolerance, tolerance)
            if not hit_bounds.contains(point):
                continue
            local = item.mapFromScene(point)
            if item.shape().contains(local):
                return edge
            if nearby is None and stroker.createStroke(item.path()).contains(local):
                nearby = edge
        return nearby

    def _graph_edges_in_rect(self, rect: QRect) -> frozenset[GraphEdge]:
        """按视口逻辑像素框选与线身或箭头相交的边，使用当前 Qt 场景中的真实形状。

        返回本次命中的有向边集合，仅供画布在 GUI 线程处理选线手势。空框及树页
        返回空集合，查询不改动选择或触发文档事件。
        """
        graph = self.graph
        if graph is None or rect.isEmpty():
            return frozenset()
        items = set(self.view.items(rect, Qt.ItemSelectionMode.IntersectsItemShape))
        return frozenset(edge for edge in graph.edges if self.branch_items.get(edge.key) in items)

    def _show_graph_edge_menu(self, global_position: QPoint) -> None:
        """冻结当前选线和图页身份，菜单确认后通过共用命令一次删除该组连线。"""
        edges = self.view.graph_edge_edit.selected_edges
        if not edges:
            return
        target = self._graph_target()
        menu = QMenu(self)
        action = menu.addAction("删除连线" if len(edges) == 1 else f"删除所选 {len(edges)} 条连线")
        action.setEnabled(self._actions["delete"].isEnabled())
        try:
            if menu.exec(global_position) is action:
                self.call(lambda: self._delete_graph_edges(edges, target=target))
        finally:
            menu.deleteLater()

    def graph_edge_context_menu(self, position: QPoint, global_position: QPoint) -> bool:
        """在 GUI 线程处理视口位置的连线右键操作，菜单使用全局屏幕位置。

        命中已选连线时保留整组，命中未选连线时单选该条。显示菜单后返回 True；
        未命中时返回 False，供调用方打开画布菜单。菜单关闭后按打开时的图页和选线校验删除。
        """
        edge = self.graph_edge_at(position)
        if edge is None:
            return False
        controller = self.view.graph_edge_edit
        if edge not in controller.selected_edges:
            controller.select_edge(edge)
        self._show_graph_edge_menu(global_position)
        return True

    def confirm_remove_graph(self) -> None:
        """确认删除当前图，重新校验页面与数据，避免确认期间删除其他标签更新后的内容。"""
        self._ensure_editable()
        self._prepare_page_navigation()
        target = self._graph_target()
        graph = self._read_active_graph(target)
        answer = QMessageBox.question(
            self, "删除此图", "删除此图？删除后可以撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._ensure_editable()
            if self._read_active_graph(target) != graph:
                raise MindMapError("确认期间图内容发生变化，请重新删除")
            self.engine.remove_node_graph(target[1])
