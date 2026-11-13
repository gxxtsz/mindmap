"""导图内的软件架构详情页，模块浏览与筛选均属于节点，不单独保存文件。"""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from mindmap_qt_editor.node_pages import NodePageContext, NodePageWidget
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QSplitter, QTabWidget, QToolBar, QVBoxLayout

from .dependency_filter_panel import DependencyFilterPanel
from .document import parse_software_structure, read_software_structure
from .module_api_panel import ModuleApiPanel
from .module_deps.canvas import ModuleDependencyCanvas
from .status_controls import SoftwareStructureStatusControls
from .template import download_template


class EmbeddedStructureDocument(QObject):
    """页面数据的只读投影；筛选写入通过上下文回到所属导图的事务。

    不拥有路径、保存计时器或撤销栈。只有宿主 set_data 回显后才更新 content，
    因此多标签、撤销、只读和保存冲突全部沿用主导图的既有行为。
    """

    edge_filter_changed = Signal(object)

    def __init__(self, context: NodePageContext, parent):
        super().__init__(parent)
        self.context = context
        self.content = None
        self.editable = True

    def set_module_edges_hidden(self, module_id: str, hidden: bool) -> bool:
        """修改指定模块的入边及出边筛选；无效模块失败且不写入导图。"""
        if module_id not in {module.id for module in self.content.modules}:
            raise ValueError(f"未找到模块：{module_id}")
        current = self.content.hidden_edge_module_ids
        return self._write_filter(current | {module_id} if hidden else current - {module_id})

    def set_all_module_edges_hidden(self, hidden: bool) -> bool:
        """批量筛选作为一个导图事务，可一次撤销。"""
        return self._write_filter({module.id for module in self.content.modules} if hidden else set())

    def _write_filter(self, hidden) -> bool:
        if not self.editable:
            return False
        if hidden == self.content.hidden_edge_module_ids:
            return True
        data = self.context.read()
        data["hidden_edge_module_ids"] = [module.id for module in self.content.modules if module.id in hidden]
        try:
            self.context.write(data)
        except (ValueError, OSError) as error:
            self.context.report(f"修改连线筛选失败：{error}")
            return False
        return True


class SoftwareStructurePage(NodePageWidget):
    """当前节点的架构画布、接口说明和筛选；所有工具放在此页内部。"""

    def __init__(self, context: NodePageContext, parent=None):
        super().__init__(parent)
        self.context = context
        self.id = uuid4().hex
        self.document = EmbeddedStructureDocument(context, self)
        self.widget: ModuleDependencyCanvas | None = None
        self._data = None
        self._editable = True
        self._disposed = False
        self.setObjectName("software_structure_node_page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.toolbar = QToolBar("软件架构工具", self)
        self.import_action = self.toolbar.addAction("导入架构数据…", self.import_dialog)
        self.template_action = self.toolbar.addAction(
            "下载格式模板…", lambda: self._call(lambda: download_template(self, context.report)))
        layout.addWidget(self.toolbar)
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.sidebar = QTabWidget(self.splitter)
        self.toggle_all = QAction("隐藏全部连线", self)
        self.filter_panel = DependencyFilterPanel(self.toggle_all)
        filter_tools = QToolBar("连线筛选", self.filter_panel)
        filter_tools.addAction(self.toggle_all)
        self.filter_panel.layout().insertWidget(0, filter_tools)
        self.api_panel = ModuleApiPanel()
        self.sidebar.addTab(self.api_panel, "模块接口")
        self.sidebar.addTab(self.filter_panel, "连线筛选")
        layout.addWidget(self.splitter, 1)
        footer = QHBoxLayout()
        self.summary = QLabel(self)
        footer.addWidget(self.summary, 1)
        self.controls = SoftwareStructureStatusControls(context)
        footer.addWidget(self.controls)
        layout.addLayout(footer)
        self.splitter.splitterMoved.connect(lambda *_: context.state_changed())
        self.sidebar.currentChanged.connect(lambda *_: context.state_changed())

    def set_data(self, data: dict) -> None:
        """校验后原地更新筛选；模块结构变化时重建画布并保留当前视口。"""
        content = parse_software_structure(data)
        previous = self.document.content
        rebuild = previous is None or (previous.modules, previous.module_dependencies, previous.module_apis) != (
            content.modules, content.module_dependencies, content.module_apis)
        self._data = deepcopy(data)
        if rebuild:
            state = self.widget.capture_viewport() if self.widget is not None else None
            sizes = self.splitter.sizes()
            self.filter_panel.bind_view(None)
            self.controls.bind_view(None)
            old = self.widget
            self.document.content = content
            self.widget = ModuleDependencyCanvas(content)
            self.splitter.insertWidget(0, self.widget)
            self.setFocusProxy(self.widget)
            self.widget.viewport_changed.connect(self.context.state_changed)
            self.widget.module_clicked.connect(self._module_clicked)
            self.document.edge_filter_changed.connect(self.widget.apply_edge_filter)
            if old is not None:
                self.document.edge_filter_changed.disconnect(old.apply_edge_filter)
                old.hide()
                old.setParent(None)
                old.deleteLater()
            # 先移除旧画布，再恢复两栏尺寸。重建中短暂存在三个子控件，此时写入
            # 两项尺寸会把真正的侧栏压成零宽，导致导入后看不到接口和筛选工具。
            self.splitter.setStretchFactor(0, 1)
            self.splitter.setStretchFactor(1, 0)
            self.splitter.setSizes(sizes if len(sizes) == 2 and sizes[1] > 0 else [900, 320])
            if state is not None:
                self.widget.restore_viewport(state)
            self.filter_panel.bind_view(self)
            self.api_panel.show_active_module(None)
        else:
            self.document.content = content
        self.document.edge_filter_changed.emit(content)
        self.controls.bind_view(self)
        self.summary.setText(f"{len(content.modules)} 个模块 · {len(content.module_dependencies)} 条依赖")
        self.api_panel.show_active_module(self)
        self.set_editable(self._editable)
        self.apply_theme()

    def import_file(self, path: str | Path) -> None:
        """把旧格式文件完整导入当前节点，不修改来源文件；导入可整体撤销。

        先按原格式校验，再保留所有业务及未知字段。文件解析、权限或事务失败时
        当前页面维持原数据，导图不会留下部分导入的模块。
        """
        if not self._editable:
            raise ValueError("当前导图为只读模式")
        _, snapshot = read_software_structure(path)
        data = json.loads(snapshot.decode("utf-8-sig"))
        self.context.write(data)
        self.context.report(f"已导入软件架构：{Path(path).name}，随当前导图保存")

    def import_dialog(self) -> None:
        """从当前节点页面选择导入来源；取消时不触发任何数据修改。"""
        if not self._editable:
            return
        filename, _ = QFileDialog.getOpenFileName(self, "导入软件架构到当前节点", "",
                                                 "软件结构 (*.software-structure)")
        if filename:
            self._call(lambda: self.import_file(filename))

    def _call(self, callback):
        try:
            callback()
        except (ValueError, OSError) as error:
            self.context.report(str(error))

    def _module_clicked(self, module_id):
        self.api_panel.show_active_module(self)
        if module_id is not None:
            self.sidebar.setCurrentWidget(self.api_panel)

    def set_editable(self, editable: bool) -> None:
        """只读时仍能浏览和临时显示全部连线，禁止持久化筛选或重新导入。"""
        self._editable = bool(editable)
        self.document.editable = self._editable
        self.import_action.setEnabled(self._editable)
        self.filter_panel.setEnabled(self._editable)
        self.toggle_all.setEnabled(self._editable and bool(self.document.content and self.document.content.modules))

    def capture_state(self) -> dict:
        """仅保存视口、侧栏尺寸和当前工具页，模块数据由主导图负责。"""
        return {"viewport": self.widget.capture_viewport() if self.widget else {},
                "sizes": self.splitter.sizes(), "sidebar": self.sidebar.currentIndex()}

    def restore_state(self, state: dict) -> None:
        if not isinstance(state, dict):
            return
        if self.widget is not None:
            self.widget.restore_viewport(state.get("viewport"))
        sizes = state.get("sizes")
        if (isinstance(sizes, list) and len(sizes) == 2
                and all(type(value) is int and 0 <= value <= 100000 for value in sizes) and sum(sizes)):
            self.splitter.setSizes(sizes)
        index = state.get("sidebar")
        if type(index) is int and 0 <= index < self.sidebar.count():
            self.sidebar.setCurrentIndex(index)

    def apply_theme(self) -> None:
        if self.widget is not None:
            self.widget.apply_theme(self.context.theme_color)
        self.filter_panel.apply_theme(self.context)
        self.api_panel.apply_theme(self.context)
        self.controls.apply_theme()

    def dispose(self) -> None:
        """解除页内文档投影及控件订阅，避免关闭页面后继续刷新旧画布。"""
        if self._disposed:
            return
        self._disposed = True
        self.filter_panel.bind_view(None)
        self.controls.bind_view(None)
        if self.widget is not None:
            self.document.edge_filter_changed.disconnect(self.widget.apply_edge_filter)
            self.widget.viewport_changed.disconnect(self.context.state_changed)
            self.widget.module_clicked.disconnect(self._module_clicked)
