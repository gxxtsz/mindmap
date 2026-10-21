"""节点属性侧栏：防抖输入、选择绑定、候选验证及批量原子提交。"""

from __future__ import annotations

from copy import deepcopy

from mindmap_core import MindMapError, apply_branch_style
from mindmap_qt_ui.properties import NodePropertiesForm, normalize_property
from mindmap_qt_ui.ui_style import sidebar_surface
from PySide6.QtCore import QTimer

from .contracts import PanelHost


class NodePropertiesPanel(NodePropertiesForm):
    """把共享表单连接到一个画布；所有未提交状态仅保存在本面板中。

    控制器不长时间持有引擎事务。每次输入等待结束才建立候选快照，通过布局验证
    后以一次事务提交，从而保留标准撤销、操作钩子、保存标记和错误回滚语义。
    """

    def __init__(self, canvas: PanelHost, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self._ids = []
        self._generation = canvas.engine.document_generation
        self._pending = {}
        self._applying = False
        self._selecting = False
        self._disposed = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self.flush)
        self.edited.connect(self._edited)
        self.flush_requested.connect(self.flush)
        self.scope_changed.connect(self._scope_changed)
        self.composition_finished.connect(self._resume_input)
        canvas.selection_changed.connect(self._selection_changed)
        canvas.document_changing.connect(self._document_changing)
        canvas.document_changed.connect(self._document_changed)
        canvas.editable_changed.connect(self._mode_changed)
        canvas.editing_changed.connect(self._mode_changed)
        canvas.branch_scope_changed.connect(self._sync_scope)
        self.sync()

    def _surface_color(self, theme):
        """侧栏背景与停靠外壳一致；保留共享表单在独立弹窗中的原有配色。"""
        return sidebar_surface(theme)

    def discard_pending(self):
        """外部撤销、重置、删除或销毁时取消计时器，禁止旧输入延迟写回。"""
        self.timer.stop()
        self._pending.clear()

    def _resume_input(self):
        if self._pending and not self._disposed:
            self.timer.start()

    def dispose(self):
        """工作区离开宿主后立即停止自动提交；Qt 信号随组件销毁自动解除。"""
        self._disposed = True
        self.discard_pending()
        # 弹层属于字段，但可能是独立顶层窗口；宿主释放工作区时立即结束其交互。
        self.fields["branch.kind"].hidePopup()

    def _mode_changed(self, value):
        self.discard_pending()
        self.sync()

    def _document_changing(self, event):
        if not self._applying and event.kind != "saved":
            self.discard_pending()

    def _document_changed(self, event):
        if not self._applying and event.kind != "saved":
            self.sync()

    def _selection_changed(self, ids):
        if self._applying or self._selecting or self._disposed:
            return
        if ids == self._ids and self._generation == self.canvas.engine.document_generation:
            return
        self._selecting = True
        try:
            # 此时画布可能已经选中新节点；flush 只使用缓存的原标识，不读取新选择。
            self.flush()
            self.sync()
        finally:
            self._selecting = False

    def sync(self):
        """从正式文档读取；自身提交期间不重建输入控件，保留光标、焦点和滚动位置。"""
        if self._disposed:
            return
        # 只读同步复用画布在提交后取得的快照；flush 的候选编辑仍使用独立副本。
        document = self.canvas.document_view
        self._generation = self.canvas.engine.document_generation
        # 外部删除正在原地编辑的节点时，结束编辑通知可能早于画布选择重建。
        # 只读取新快照中仍存在的标识，随后正式选择通知会再次同步最终选择。
        self._ids = [key for key in self.canvas.selected_node_ids() if key in document.nodes]
        self.set_theme(document.theme)
        self.load_nodes(
            [document.nodes[key] for key in self._ids],
            root_id=document.root_id,
            parents=self.canvas.parent_index,
            editable=self.canvas.editable and not self.canvas.is_editing,
            branch_styles=self.canvas.layout_registry.info(
                document.layout.algorithm_id
            ).supports_branch_styles,
            branch_scope=self.canvas.branch_scope,
            branch_kinds=self.canvas.branch_kinds(self._ids, self.canvas.branch_scope),
        )

    def _scope_changed(self, scope):
        self.canvas.set_branch_scope(scope)

    def _sync_scope(self, scope):
        loading, self._loading = self._loading, True
        try:
            self.scope.setCurrentIndex(self.scope.findData(scope))
        finally:
            self._loading = loading
        self.set_branch_kinds(self.canvas.branch_kinds(self._ids, scope))

    def _edited(self, key, value, immediate):
        if self._disposed or not self.canvas.editable or self.canvas.is_editing or not self._ids:
            return
        self._pending[key] = value
        if immediate:
            self.flush()
        else:
            self.timer.start()

    def flush(self):
        """提交当前原节点的有效字段；无效字段只显示错误，不影响其他有效修改。

        候选布局验证成功后仍通过公开引擎操作提交，不能直接替换引擎内部状态。
        批量样式分别合并每个节点自己的字典；分支请求单独处理，未触碰它时不修改后代。
        """
        if self._applying or self._disposed or self.composing:
            return
        self.timer.stop()
        if not self._pending:
            return
        engine = self.canvas.engine
        if (
            self._generation != engine.document_generation
            or not self.canvas.editable
            or self.canvas.is_editing
        ):
            self.discard_pending()
            return
        document = engine.document
        if any(key not in document.nodes for key in self._ids):
            self.discard_pending()
            return
        pending, self._pending = self._pending, {}
        changes = {}
        for key, value in pending.items():
            try:
                changes[key] = normalize_property(key, value)
            except ValueError as exc:
                self.set_error(key, str(exc))
        if not changes:
            return
        candidate, patches = deepcopy(document), {}
        try:
            for node_id in self._ids:
                node, patch = candidate.nodes[node_id], {}
                style = deepcopy(node.style)
                for key, value in changes.items():
                    if key == "branch.kind":
                        continue
                    if key.startswith("style."):
                        name = key.split(".")[1]
                        if value == "":
                            style.pop(name, None)
                        else:
                            style[name] = value
                    else:
                        patch[key] = deepcopy(value)
                if style != node.style:
                    patch["style"] = style
                patch = {key: value for key, value in patch.items() if getattr(node, key) != value}
                for key, value in patch.items():
                    setattr(node, key, deepcopy(value))
                patches[node_id] = patch
            if "branch.kind" in changes:
                if not self.canvas.layout_registry.info(document.layout.algorithm_id).supports_branch_styles:
                    raise MindMapError("当前布局算法不支持分支连接线样式")
                apply_branch_style(
                    candidate, self._ids, changes["branch.kind"], scope=self.canvas.branch_scope
                )
            self.canvas.validate_document(candidate)
            self._applying = True
            with engine.transaction("修改节点属性"):
                for node_id, patch in patches.items():
                    if patch:
                        engine.update_node(node_id, **patch)
                if "branch.kind" in changes:
                    engine.set_branch_style(
                        self._ids, changes["branch.kind"], scope=self.canvas.branch_scope
                    )
            self.canvas.sync_branch_actions()
            if len(self._ids) == 1:
                self.summary.setText((engine.get_node(self._ids[0]).text.splitlines() or ["未命名节点"])[0])
        except (MindMapError, ValueError, KeyError) as exc:
            for key in changes:
                self.set_error(key, str(exc))
            self.canvas.error_occurred.emit(str(exc))
        finally:
            self._applying = False
