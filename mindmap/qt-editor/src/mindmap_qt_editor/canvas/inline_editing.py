"""画布正文的原地编辑、草稿预览及确认。"""

from collections import ChainMap

from PySide6.QtCore import QRectF, Qt

from .items import NodeEditor


class InlineEditingMixin:
    """在 GUI 线程管理画布拥有的原地编辑器和临时几何。

    草稿期间只调整显示，确认后交给引擎记录撤销。宿主可查询候选状态，
    并在切页、切换焦点或关闭时通过画布确认或放弃草稿。
    """

    @property
    def is_editing(self) -> bool:
        """返回是否有原地文字草稿，宿主无需取得或操作内部 NodeEditor。"""
        return self.editor is not None

    @property
    def is_composing(self) -> bool:
        """只读查询原地编辑器是否还有输入法候选；没有编辑器时返回 False。

        宿主在保存、另存或关闭前查询，不需要持有内部 NodeEditor。查询不提交
        候选、不结束草稿、不改变焦点；候选完成后再调用正常的输入提交接口。
        """
        return self.editor is not None and self.editor.composing

    def begin_edit(self, node_id: str) -> None:
        """为当前页可见的普通节点开始正文编辑，返回 None。

        node_id 为节点标识；只读、不可见或纯图片节点直接返回，不提交已有草稿。
        成功时先提交原编辑器，再建立实时预览换行和尺寸的新编辑器；文字最终提交
        只生成一次引擎操作。提交错误向上传递。仅在 GUI 线程、画布存活时使用，
        编辑器由本画布和场景管理，调用方不得在结束编辑或切页后保留其引用。
        """
        if (not self.editable or self.is_composing or node_id not in self.layout_result.nodes
                or self._document.nodes[node_id].is_image_node
                or self._document.nodes[node_id].page_reference_root_id is not None):
            return
        self.finish_edit(commit=True)
        self._cancel_branch_menu()
        self.select_nodes([node_id])
        item = self.node_items[node_id]
        item.editing = True
        item.update()
        node = self._document.nodes[node_id] if self.graph is not None else self.engine.get_node(node_id)
        self.editor = NodeEditor(self, node, item.visual)
        self.editor.setPos(item.pos() + item.visual.text_position)
        self.scene.addItem(self.editor)
        self.editor.sync_geometry()
        self.editing_changed.emit(True)
        self.view.setFocus()
        self.editor.setFocus(Qt.FocusReason.OtherFocusReason)
        self._update_actions()
        self.view.update_cursor()

    def _update_edit_preview(self, editor, *, force=False):
        """用草稿尺寸试算显示布局，保留正式测量、增量索引及文档历史。

        内置布局在独立持久化快照上只更新一个节点及其祖先；自定义布局仍走
        注册表入口。节点尺寸没有变化时只替换视觉，不重复排布其他节点。
        """
        visual = editor.preview_visual
        item = self.node_items[editor.node_id]
        previous_size = item.visual.width, item.visual.height
        item.update_content(self._document.nodes[editor.node_id], visual, self._document.theme)
        item.sync_fold_control()
        if force or previous_size != (visual.width, visual.height):
            sizes = ChainMap({editor.node_id: (visual.width, visual.height)}, self._measurements)
            if self.graph is not None:
                result = self._calculate_graph_layout(sizes)
            elif self._incremental_layout is not None:
                result = self._incremental_layout.preview_measurements(self._document, sizes, (editor.node_id,))
            else:
                result = self.layout_registry.layout(self._document, sizes, focus_id=self.focus_id)
            self._show_edit_layout(result)
        editor.setPos(item.pos() + visual.text_position)
        # 节点变宽后可能越出原视口；能放下时只做必要平移，保证整个编辑外框
        # 可见。正文高于视口时跟随原生光标矩形，避免长文本把输入位置挤出屏幕。
        scale = self.view.transform().m11()
        bounds = item.sceneBoundingRect()
        viewport = self.view.viewport().rect()
        if bounds.width() * scale + 48 <= viewport.width() and bounds.height() * scale + 48 <= viewport.height():
            self.view.ensureVisible(item, 24, 24)
        else:
            cursor = QRectF(editor.inputMethodQuery(Qt.InputMethodQuery.ImCursorRectangle))
            self.view.ensureVisible(editor.mapRectToScene(cursor), 24, 24)

    def _show_edit_layout(self, result):
        """同步临时节点及连接线，保持倍率和视口中心；结束编辑时也复用此处恢复。

        Qt 更新场景范围可能同步触发滚动回调，批量更新期间先屏蔽视口同步及
        选择信号，避免混用新旧布局。大图仍只实例化屏内节点和正在编辑的节点。
        """
        center = self.view.mapToScene(self.view.viewport().rect()).boundingRect().center()
        self.layout_result = result
        blocked = self.scene.blockSignals(True)
        self._refreshing = True
        try:
            bounds = result.bounds
            self.scene.setSceneRect(QRectF(bounds.x, bounds.y, bounds.width, bounds.height))
            self.view.update_navigation_rect(center=center)
            if self._virtualized:
                self._sync_viewport(force=True)
            else:
                self._set_node_items(self._document, self._visuals, result)
                self._set_branch_items(result)
        finally:
            self._refreshing = False
            self.scene.blockSignals(blocked)

    def finish_edit(self, *, commit=True):
        """移除草稿并恢复正式几何后提交或取消；候选未完成时拒绝提交并保留编辑器。

        commit=False 仍允许宿主显式放弃编辑。整次有效正文提交只产生一条修改记录，
        失焦或其他标签的保存检查不得把输入法候选当作已确认文字。
        仅在本视图原本持有键盘焦点时恢复画布焦点；失焦提交及后台保存必须保留
        用户刚点击的其他画布或侧栏焦点，避免后续快捷键重新作用于旧编辑器。
        """
        editor = self.editor
        if editor is None or (commit and editor.composing):
            return
        # 文字图元的失焦回调延迟到下一轮事件循环；此时用户可能已经切换画布，
        # 必须在移除图元和提交引擎更新前记录焦点归属，不能无条件聚焦旧视图。
        restore_view_focus = self.view.hasFocus()
        editor.finishing = True
        node_id, text = editor.node_id, editor.toPlainText()
        self.editor = None
        self.scene.removeItem(editor)
        editor.deleteLater()
        if node_id in self.node_items:
            self.node_items[node_id].editing = False
            self.node_items[node_id].update()
        if not self._disposed:
            self._show_edit_layout(editor.base_layout)
        if commit and self.editable:
            if self.graph is not None:
                self.call(lambda: self._rename_graph_node(node_id, text))
            else:
                self.call(lambda: self.engine.update_node(node_id, text=text))
        self._update_actions()
        if restore_view_focus:
            self.view.setFocus()
        self.view.update_cursor()
        self.editing_changed.emit(False)

    def edit_selected(self):
        """编辑当前节点的正文；具体只读与输入法处理由节点编辑器负责。"""
        self.begin_edit(self.current_node_id())
