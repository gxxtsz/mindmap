"""只使用公开协议连接普通 QWidget，验证面板不要求真实画布或场景。"""

from types import MappingProxyType

from mindmap_engine import (
    MindMapEngine,
    MindMapError,
    branch_style_targets,
    create_default_layout_registry,
    get_branch_style,
)
from mindmap_qt_panels import MindMapPanels
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget


class IndependentHost(QWidget):
    """测试用独立宿主，不继承或导入画布，通过引擎及公开协议承载全部面板。

    借用文档只在引擎通知时更新；选择和滚轮属于宿主状态。没有图形场景，因此
    reveal_nodes 仅记录导航请求，实际几何及视口策略由真正的图形宿主自行实现。
    """

    selection_changed = Signal(list)
    document_changing = Signal(object)
    document_changed = Signal(object)
    editable_changed = Signal(bool)
    editing_changed = Signal(bool)
    branch_scope_changed = Signal(str)
    wheel_mode_changed = Signal(str)
    edits_flush_requested = Signal()
    error_occurred = Signal(str)

    def __init__(self):
        super().__init__()
        self.engine = MindMapEngine()
        self.layout_registry = create_default_layout_registry()
        self.document_view = self.engine.document
        self.editable = True
        self.is_editing = False
        self.branch_scope = "subtree"
        self.wheel_mode = "scroll"
        self.selection = [self.engine.root_id]
        self.revealed = []
        self.unsubscribe = self.engine.subscribe(self.changed)

    def changed(self, event):
        """遵循先通知取消旧输入、再更新借用视图、最后通知同步控件的事件顺序。"""
        self.document_changing.emit(event)
        if event.kind != "saved":
            if event.changes is None:
                self.document_view = self.engine.document
            else:
                event.changes.apply(self.document_view, forward=event.forward)
        self.document_changed.emit(event)

    @property
    def parent_index(self):
        """仅向消费者暴露只读父索引，测试宿主无需提供任何画布私有字段。"""
        return MappingProxyType(self.document_view.parents())

    def selected_node_ids(self):
        """返回选择副本，避免面板改写宿主的当前选择列表。"""
        return list(self.selection)

    def reveal_nodes(self, ids):
        """提交有效输入后记录导航请求；通过选择信号驱动面板同步。"""
        self.edits_flush_requested.emit()
        self.revealed = self.selection = list(ids)
        self.selection_changed.emit(self.selection)

    def validate_document(self, document):
        """通过布局注册表校验候选，不向正式引擎写入候选状态。"""
        self.layout_registry.layout(document)

    def branch_kinds(self, ids, scope="subtree"):
        """按公开领域接口统计样式，保持与画布协议一致的混合值语义。"""
        return {get_branch_style(self.document_view.nodes[key].style)
                for key in branch_style_targets(self.document_view, ids, scope=scope)}

    def set_branch_scope(self, scope):
        """只更新界面范围及通知，不创建引擎历史。"""
        if scope not in {"level", "subtree"}:
            raise MindMapError("分支范围无效")
        self.branch_scope = scope
        self.branch_scope_changed.emit(scope)

    def sync_branch_actions(self):
        """普通宿主没有画布动作；协议允许在此进行空操作。"""

    def set_wheel_mode(self, mode):
        """更新宿主偏好并通知设置菜单，不修改文档。"""
        self.wheel_mode = mode
        self.wheel_mode_changed.emit(mode)

    def call(self, callback):
        """沿用宿主错误信号边界，面板无需弹出自己的错误对话框。"""
        try:
            return callback()
        except (MindMapError, ValueError) as error:
            self.error_occurred.emit(str(error))


def test_panels_edit_and_navigate_without_canvas(qtbot):
    """属性、全局设置、大纲导航及撤销均可在没有场景图元的宿主上工作。"""
    host = IndependentHost()
    qtbot.addWidget(host)
    panels = MindMapPanels(host)
    try:
        panels.properties.fields["style.font_size"].setValue(24)
        panels.flush_pending_edits()
        assert host.engine.get_node(host.engine.root_id).style["font_size"] == 24
        assert host.engine.undo()
        assert "font_size" not in host.engine.get_node(host.engine.root_id).style
        index = panels.settings.direction.findData("down")
        panels.settings.direction.setCurrentIndex(index)
        panels.settings.direction.activated.emit(index)
        assert host.document_view.layout.direction == "down"
        child = host.engine.add_child(host.engine.root_id, "独立面板导航")
        panels.outline.nodes_selected.emit([child])
        assert host.revealed == [child] and panels.properties._ids == [child]
        panels.dispose()
        panels.dispose()
    finally:
        panels.dispose()
        host.unsubscribe()
