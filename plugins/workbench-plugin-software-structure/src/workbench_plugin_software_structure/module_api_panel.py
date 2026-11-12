"""在独立的只读侧栏中展示活动标签所选模块的职责和接口用途。"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from workbench_api import Disposable, EditorView, PluginContext
from workbench_api.qt import PanelSpec, UiService

from .document import Module, ModuleApi, SoftwareStructure
from .viewer import MODULE_API_PANEL_ID, SoftwareStructureView


class ModuleApiPanel(QWidget):
    """用可换行的单列显示模块职责、接口及可展开的类成员。"""

    def __init__(self) -> None:
        super().__init__()
        self._selection: tuple[str, str | None] | None = None
        self.setObjectName("software_structure_module_api_panel")
        self.setAccessibleName("模块接口")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)

        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("module_api_scroll")
        self._scroll.setAccessibleName("模块职责和接口说明")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusProxy(self._scroll)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._scroll)

    def apply_theme(self, ui: UiService) -> None:
        """从宿主主题获取侧栏配色。主题更新时保留滚动位置和已展开的成员。"""
        surface = ui.theme_color("tool-window-bg").name()
        border = ui.theme_color("tool-window-border").name()
        text = ui.theme_color("text-default").name()
        muted = ui.theme_color("text-muted").name()
        hover = ui.theme_color("toolbar-bg-hovered").name()
        focus = ui.theme_color("control-brand-border").name()
        self.setStyleSheet(f"""
            QWidget#software_structure_module_api_panel, QWidget#module_api_content,
            QScrollArea#module_api_scroll {{ background: {surface}; }}
            QLabel {{ color: {text}; background: transparent; }}
            QLabel#module_api_module_name {{ font-weight: bold; font-size: 13pt; }}
            QLabel#module_api_name {{ font-weight: bold; }}
            QLabel#module_api_hint {{ color: {muted}; }}
            QFrame#module_api_divider {{ background: {border}; }}
            QToolButton#module_api_members_toggle {{
                color: {text}; background: transparent; border: 1px solid transparent;
                border-radius: 4px; padding: 0;
            }}
            QToolButton#module_api_members_toggle:hover {{ background: {hover}; }}
            QToolButton#module_api_members_toggle:focus {{ border-color: {focus}; }}
        """)

    def show_active_module(self, view: SoftwareStructureView | None) -> None:
        """按标签和模块标识切换内容。重复查看同一选择时保留已展开的类和阅读位置。

        标签可以共享文档，但选中的模块由各自的画布持有。切换选择时替换滚动内容，
        使旧列表及其按钮随原控件释放，新列表从顶部和折叠状态开始显示。
        """
        module_id = view.widget.selected_module_id if view is not None else None
        selection = (view.id, module_id) if view is not None else None
        if self._scroll.widget() is not None and selection == self._selection:
            return

        content = QWidget()
        content.setObjectName("module_api_content")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 10, 14, 14)
        layout.setSpacing(12)
        if view is not None and module_id is not None:
            document = view.document.content
            module = next(module for module in document.modules if module.id == module_id)
            self._add_module_details(layout, document, module)
        else:
            hint = "点击画布中的模块查看接口"
            if view is not None and not view.document.content.modules:
                hint = "文件中暂无模块"
            layout.addWidget(self._label(hint, "module_api_hint"))
        layout.addStretch(1)
        self._scroll.setWidget(content)
        self._scroll.verticalScrollBar().setValue(0)
        self._selection = selection

    def _add_module_details(self, layout: QVBoxLayout, document: SoftwareStructure, module: Module) -> None:
        """把模块名和职责置顶，并区分缺少接口说明与显式空清单。"""
        heading = QVBoxLayout()
        heading.setSpacing(4)
        heading.addWidget(self._label(module.name, "module_api_module_name"))
        if module.description.strip():
            heading.addWidget(self._label(module.description, "module_api_module_description"))
        else:
            heading.addWidget(self._label("暂无职责说明", "module_api_hint"))
        layout.addLayout(heading)

        divider = QFrame()
        divider.setObjectName("module_api_divider")
        divider.setFixedHeight(1)
        layout.addWidget(divider)

        group = next((group for group in document.module_apis if group.module_id == module.id), None)
        if group is None:
            layout.addWidget(self._label("文件未提供接口说明", "module_api_hint"))
        elif not group.apis:
            layout.addWidget(self._label("暂无符合收录范围的接口", "module_api_hint"))
        else:
            for api in group.apis:
                layout.addWidget(self._api_row(api))

    def _api_row(self, api: ModuleApi) -> QWidget:
        """为单条接口创建名称和用途。首次展开时才生成该类的成员控件。"""
        row = QWidget()
        layout = QVBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        name = self._label(api.name, "module_api_name")
        if not api.members:
            layout.addWidget(name)
            layout.addWidget(self._label(api.description, "module_api_description"))
            return row

        header = QHBoxLayout()
        header.setSpacing(6)
        header.addWidget(name, 1, Qt.AlignmentFlag.AlignVCenter)
        toggle = QToolButton()
        toggle.setObjectName("module_api_members_toggle")
        toggle.setArrowType(Qt.ArrowType.RightArrow)
        toggle.setCheckable(True)
        toggle.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        toggle.setFixedSize(24, 24)
        toggle.setAccessibleName(f"展开「{api.name}」的成员")
        toggle.setToolTip("展开成员")
        header.addWidget(toggle, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)
        layout.addWidget(self._label(api.description, "module_api_description"))

        members = QWidget()
        member_layout = QVBoxLayout(members)
        member_layout.setContentsMargins(14, 4, 0, 2)
        member_layout.setSpacing(8)
        members.hide()
        layout.addWidget(members)

        def show_members(expanded: bool) -> None:
            if expanded and not member_layout.count():
                for member in api.members:
                    member_layout.addWidget(self._api_row(member))
            members.setVisible(expanded)
            toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
            action = "收起" if expanded else "展开"
            toggle.setAccessibleName(f"{action}「{api.name}」的成员")
            toggle.setToolTip(f"{action}成员")

        toggle.toggled.connect(show_members)
        return row

    @staticmethod
    def _label(text: str, object_name: str) -> QLabel:
        """以纯文本显示文件内容，允许窄侧栏缩小宽度并按可用空间换行。"""
        label = QLabel()
        label.setObjectName(object_name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setText(text)
        label.setWordWrap(True)
        policy = label.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        label.setSizePolicy(policy)
        return label


def register_module_api_panel(context: PluginContext) -> Disposable:
    """登记独立的右下侧栏，并在活动标签变化时切换画布订阅。

    宿主延迟创建控件。首次展开时从活动画布读取选中模块，之后仅跟随活动标签的点击。
    离开软件结构标签时暂停面板，显隐和停靠位置由宿主保留，注销时断开所有订阅。
    """
    panel: ModuleApiPanel | None = None
    active: SoftwareStructureView | None = None

    def refresh_selection(*_) -> None:
        if panel is not None:
            panel.show_active_module(active)

    def create_panel() -> QWidget:
        nonlocal panel
        panel = ModuleApiPanel()
        panel.apply_theme(context.ui)
        panel.show_active_module(active)
        return panel

    def bind_view(view: EditorView | None) -> None:
        """解绑旧画布并同步新画布已有的选择。切换标签本身不触发展开。"""
        nonlocal active
        if active is not None:
            active.widget.module_clicked.disconnect(refresh_selection)
        active = view if isinstance(view, SoftwareStructureView) else None
        if active is not None:
            active.widget.module_clicked.connect(refresh_selection)
        refresh_selection()
        context.ui.set_panels_suspended((MODULE_API_PANEL_ID,), active is None)

    def refresh_theme(*_) -> None:
        if panel is not None:
            panel.apply_theme(context.ui)

    context.ui.register_panel(PanelSpec(
        id=MODULE_API_PANEL_ID, title="模块接口", factory=create_panel, icon="outline", position="right_bottom",
    ))
    detach = context.own(lambda: bind_view(None))
    subscription = context.ui.subscribe_active(bind_view)
    theme_subscription = context.ui.subscribe_theme(refresh_theme)
    bind_view(context.documents.active)

    def dispose() -> None:
        theme_subscription.dispose()
        subscription.dispose()
        detach.dispose()

    return Disposable(dispose)
