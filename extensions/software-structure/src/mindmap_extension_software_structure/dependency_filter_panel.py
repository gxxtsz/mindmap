"""节点详情页内部的连线筛选面板，修改结果统一交回所属导图事务。"""

from html import escape

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from .document import SoftwareStructure


class DependencyFilterPanel(QWidget):
    """按模块顺序显示筛选，回显所属节点最近一次提交的内容。"""

    def __init__(self, toggle_all_action: QAction):
        """创建模块列表，绑定专用页面提供的批量操作，不注册全局停靠面板。"""
        super().__init__()
        self._view: object | None = None
        self._toggle_all_action = toggle_all_action
        self._toggle_all_action.triggered.connect(self._toggle_all_modules)
        self.setObjectName("software_structure_dependency_filter_panel")
        self.setAccessibleName("连线筛选")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 12)
        layout.setSpacing(8)

        self._hint = QLabel("文件中暂无模块", self)
        self._hint.setWordWrap(True)
        self._hint.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._hint)

        self._module_list = QListWidget(self)
        self._module_list.setObjectName("dependency_filter_modules")
        self._module_list.setAccessibleName("连线筛选模块列表")
        self._module_list.setAccessibleDescription("选中模块后按空格切换勾选。勾选后隐藏与该模块相连的线。")
        self._module_list.setUniformItemSizes(True)
        self._module_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._module_list.itemChanged.connect(self._save_module)
        self._module_list.hide()
        self.setFocusProxy(self._module_list)
        layout.addWidget(self._module_list, 1)

    def apply_theme(self, ui) -> None:
        """用宿主色板刷新面板背景和模块列表，保留勾选状态、焦点与滚动位置。"""
        surface = ui.theme_color("tool-window-bg").name()
        text = ui.theme_color("text-default").name()
        muted = ui.theme_color("text-muted").name()
        hover = ui.theme_color("toolbar-bg-hovered").name()
        selected = ui.theme_color("toolbar-selected-bg-active").name()
        self.setStyleSheet(f"""
            QWidget#software_structure_dependency_filter_panel, QListWidget {{ background: {surface}; }}
            QLabel {{ color: {muted}; background: transparent; }}
            QListWidget {{ color: {text}; border: 0; }}
            QListWidget::item {{ padding: 4px; border-radius: 4px; }}
            QListWidget::item:hover {{ background: {hover}; }}
            QListWidget::item:selected {{ color: {text}; background: {selected}; }}
        """)

    def bind_view(self, view: object | None) -> None:
        """绑定活动标签，列表和标题按钮回显该文件的筛选设置。

        切换文件时解除旧文档订阅。同文件的标签共用列表，仍要更新活动标签引用，
        以便保存后结束正确标签的预览。传入 None 时清空列表并禁用批量操作。
        """
        previous = self._view.document if self._view is not None else None
        self._view = view
        document = view.document if view is not None else None
        if document is previous:
            return
        if previous is not None:
            previous.edge_filter_changed.disconnect(self._sync_checks)

        content = document.content if document is not None else None
        modules = content.modules if content is not None else ()
        with QSignalBlocker(self._module_list):
            self._module_list.clear()
            for module in modules:
                name = " ".join(module.name.splitlines())
                item = QListWidgetItem(name)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
                item.setData(Qt.ItemDataRole.UserRole, module.id)
                item.setData(Qt.ItemDataRole.AccessibleTextRole, f"{name}（ID：{module.id}）")
                item.setToolTip(f"<qt>{escape(module.name)}<br>ID：{escape(module.id)}</qt>")
                self._module_list.addItem(item)
        self._sync_checks(content)
        self._module_list.setVisible(bool(modules))
        self._hint.setText("勾选要隐藏连线的模块" if modules else "文件中暂无模块")
        if document is not None:
            document.edge_filter_changed.connect(self._sync_checks)

    def _sync_checks(self, content: SoftwareStructure | None) -> None:
        """从已保存的快照回显勾选状态和标题动作，阻断列表信号以免重复保存。

        部分勾选或未勾选时提示隐藏全部连线，全部勾选时提示清除筛选。
        """
        modules = content.modules if content is not None else ()
        hidden = content.hidden_edge_module_ids if content is not None else frozenset()
        with QSignalBlocker(self._module_list):
            for row, module in enumerate(modules):
                state = Qt.CheckState.Checked if module.id in hidden else Qt.CheckState.Unchecked
                self._module_list.item(row).setCheckState(state)
        all_checked = bool(modules) and len(hidden) == len(modules)
        self._toggle_all_action.setEnabled(bool(modules))
        action = "清除筛选" if all_checked else "隐藏全部连线"
        self._toggle_all_action.setText(action)
        self._toggle_all_action.setToolTip(action)

    def _toggle_all_modules(self) -> None:
        """按最近保存的勾选决定批量操作，成功后让发起编辑的标签应用筛选。"""
        view = self._view
        if view is not None:
            content = view.document.content
            hidden = len(content.hidden_edge_module_ids) < len(content.modules)
            if view.document.set_all_module_edges_hidden(hidden):
                view.widget.set_show_all_dependencies(False)

    def _save_module(self, item: QListWidgetItem) -> None:
        """提交单项勾选。成功后结束发起标签的预览，失败时回显最近保存的勾选。"""
        view = self._view
        if view is not None:
            module_id = item.data(Qt.ItemDataRole.UserRole)
            hidden = item.checkState() == Qt.CheckState.Checked
            if view.document.set_module_edges_hidden(module_id, hidden):
                view.widget.set_show_all_dependencies(False)
            else:
                self._sync_checks(view.document.content)
