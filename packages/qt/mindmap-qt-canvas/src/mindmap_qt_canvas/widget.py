"""可嵌入宿主窗口的完整思维导图组件。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from types import MappingProxyType

from mindmap_core import (
    BranchStyle,
    FormatRegistry,
    LayoutConfig,
    LayoutRegistry,
    LayoutResult,
    MindMapDocument,
    MindMapEngine,
    MindMapError,
    Rect,
    apply_branch_style,
    branch_style_targets,
    get_branch_style,
)
from mindmap_formats import create_default_format_registry, decode_document, encode_document
from mindmap_layouts import TreeLayout, create_default_layout_registry
from mindmap_layouts.incremental_result import IncrementalTreeLayout
from mindmap_qt_render.exporting import export_document
from mindmap_qt_render.rendering import BranchItem, VisualFactory, decode_image
from mindmap_qt_ui.branch_styles import BranchStyleMenu, branch_style_icon
from mindmap_qt_ui.dialogs import edraw_paste_options, node_properties
from mindmap_qt_ui.icons import (
    IconMenuOptionButton,
    IconOptionMenu,
    add_icon_action,
    apply_toolbar_theme,
    set_action_icon,
)
from mindmap_qt_ui.ui_style import CardFrame, UiColors, apply_ui_palette, dialog_styles, ui_colors
from PySide6.QtCore import QMimeData, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QKeySequence
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGraphicsScene,
    QInputDialog,
    QMenu,
    QToolBar,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from .clipboard import EDRAW_CLIPBOARD_MIME, read_edraw_clipboard
from .items import NodeEditor, NodeItem
from .view import MIN_ZOOM, MindMapView
from .view_controls import _ViewControls

CLIPBOARD_MIME = "application/x-python-mindmap+json"
VIRTUALIZATION_NODE_LIMIT = 1000


class MindMapWidget(QWidget):
    """原生画布组件。

    宿主先创建 QApplication，再将本组件加入任意布局。engine 可省略，此时创建
    独立引擎。只读模式约束组件发起的修改，宿主仍可通过引擎加载或更新文档。
    所有图形接口应在 GUI 线程使用，使用 dispose() 可主动解除订阅和网络请求。
    """

    selection_changed = Signal(list)
    document_changed = Signal(object)
    history_changed = Signal(bool, bool, bool)
    error_occurred = Signal(str)
    image_imported = Signal(str)
    wheel_mode_changed = Signal(str)
    edraw_paste_options_changed = Signal(str, bool)
    editable_changed = Signal(bool)
    editing_changed = Signal(bool)
    document_changing = Signal(object)
    edits_flush_requested = Signal()
    branch_scope_changed = Signal(str)

    def __init__(
        self,
        engine: MindMapEngine | None = None,
        parent=None,
        *,
        editable=True,
        toolbar=True,
        view_controls=False,
        embedded=False,
        layout_registry: LayoutRegistry | None = None,
        format_registry: FormatRegistry | None = None,
    ):
        """构造画布并注入实例级布局和格式能力，省略时建立独立默认注册表。

        toolbar 控制传统顶部工具栏是否可见；view_controls=True 在视口右下角
        提供适应窗口和回到中心，适合由宿主接管历史动作的布局。两项可以独立
        配置，默认保持独立组件原有布局。浮条复用已有动作，由画布随 Qt 父子
        关系销毁，不创建场景元素，不改变导图内容、快捷键或图形导出。

        embedded=True 表示宿主提供编辑器外层圆角：移除内部卡片圆角和留白，
        并用界面色板的 field 作为显示背景。仅影响视图，文档主题和导出保持原值。
        """
        super().__init__(parent)
        self.setObjectName("mindmap_canvas")
        self.setAutoFillBackground(True)
        self._ui_colors = None
        self._ui_colors_override = None
        self._embedded = embedded
        self.engine = engine or MindMapEngine()
        self.editable = editable
        self._properties_handler = None
        self._wheel_mode = "scroll"
        self._edraw_paste_options = ("append", True)
        self.factory = VisualFactory()
        self.layout_registry = (
            layout_registry if layout_registry is not None else create_default_layout_registry()
        )
        self.format_registry = (
            format_registry if format_registry is not None else create_default_format_registry()
        )
        self._disposed = False
        self._view_reset_pending = False
        # 初始化属于画布自身的生命周期；复用一个单次定时器，让打开文件、首次
        # 显示和连续布局事件合并为一次定位，并在组件销毁时由 Qt 自动取消回调。
        self._view_reset_timer = QTimer(self)
        self._view_reset_timer.setSingleShot(True)
        self._view_reset_timer.timeout.connect(self._apply_view_reset)
        self.scene = QGraphicsScene(self)
        self.view = MindMapView(self, self.scene)
        self.toolbar = QToolBar("思维导图工具栏", self)
        self.toolbar.setMovable(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.toolbar)
        # 圆角只属于界面承载层；视口与场景仍是原生矩形，导出不读取卡片外观。
        self._view_card = CardFrame(self.view, self, rounded=not embedded)
        layout.addWidget(self._view_card)
        self.toolbar.setVisible(toolbar)
        self.node_items, self.branch_items = {}, {}
        self.editor = None
        self.focus_id = None
        self._refreshing = False
        self._branch_scope = "subtree"
        self._preview_layout = None
        self._measurements = {}
        self._measure_dirty = None
        self._visual_document = None
        self._visuals = {}
        self._incremental_layout = None
        self._incremental_key = None
        self._virtualized = False
        self._viewport_signature = None
        self._selection_undo, self._selection_redo = [], []
        self._document = self.engine.document
        self._node_parents = self._document.parents()
        self._node_order = None
        self._actions = {}
        self._mutating = set()
        self._custom_actions = []
        self.network = QNetworkAccessManager(self)
        self._downloads = set()
        self._create_actions()
        self._view_controls = (
            _ViewControls(self.view, [self.action("fit"), self.action("center")])
            if view_controls else None
        )
        self.scene.selectionChanged.connect(self._selection_changed)
        self._unsubscribe = self.engine.subscribe(self._engine_changed)
        # 捕获取消函数而不捕获 QWidget，确保宿主删除组件时引擎不会继续持有绑定方法。
        unsubscribe = self._unsubscribe
        self.destroyed.connect(lambda: unsubscribe())
        self.refresh()
        self.select_nodes([self.engine.root_id])
        self.reset_view()

    def dispose(self):
        """主动释放订阅与下载；多次调用安全，通常用于宿主移除标签页。"""
        if self._disposed:
            return
        self._cancel_branch_menu(restore=False)
        self.view.cancel_interaction()
        self._disposed = True
        self._view_reset_pending = False
        self._view_reset_timer.stop()
        self._unsubscribe()
        for reply in tuple(self._downloads):
            reply.abort()

    @property
    def document_view(self) -> MindMapDocument:
        """借用当前界面文档供面板只读查询，不建立整图副本。

        内容由引擎提交事件增量维护；调用方不得修改、也不得在文档替换后继续使用
        旧引用。需要编辑或验证候选时，应另取 engine.document 的独立快照。
        """
        return self._document

    @property
    def parent_index(self) -> Mapping[str, str]:
        """返回当前直接父节点索引的只读代理；构造代理不遍历或复制整棵树。

        根节点没有父条目。每次文档事件处理后重新查询本属性，避免跨重置缓存旧索引。
        """
        return MappingProxyType(self._node_parents)

    @property
    def is_editing(self) -> bool:
        """返回是否有原地文字草稿，宿主无需取得或操作内部 NodeEditor。"""
        return self.editor is not None

    @property
    def branch_scope(self) -> str:
        """返回局部分支编辑范围；subtree 表示整个子树，level 表示仅本级。"""
        return self._branch_scope

    def set_branch_scope(self, scope: str) -> None:
        """切换局部分支范围并同步动作、预览和面板；不修改文档或撤销历史。

        只接受 subtree、level，非法输入由原有校验拒绝，保持之前的范围。
        """
        self._set_branch_scope(scope)

    def branch_kinds(self, node_ids: Sequence[str], scope: str = "subtree") -> set[BranchStyle | None]:
        """查询指定节点范围的分支样式集合，复用现有增量摘要。

        None 表示原始曲线，多项表示混合样式。仅用于查询，不创建预览或修改历史。
        """
        return self._branch_kinds(node_ids, scope)

    def sync_branch_actions(self) -> None:
        """在外部属性事务结束后刷新分支动作展示，不提交新的引擎操作。"""
        self._sync_branch_actions()

    @property
    def canvas_surface(self) -> QWidget:
        """取得画布界面承载层，供组合工作区配置尺寸及外观；不属于导出场景。"""
        return self._view_card

    def cancel_transient_ui(self) -> None:
        """关闭未提交的分支菜单及预览；标签切换前调用，不撤销已经提交的修改。"""
        self._cancel_branch_menu()

    def reveal_nodes(self, node_ids: Sequence[str]) -> None:
        """将完整树中的节点定位到视口，供大纲及其他宿主导航入口共同使用。

        先请求面板提交有效输入，再结束原地编辑。隐藏祖先的展开共用一条历史；
        聚焦退出和视口移动属于临时状态。只读时需要展开、标识无效或布局拒绝会
        抛出异常，调用方负责通过自己的错误入口展示，不能绕过这里直接操作图元。
        """
        ids = list(node_ids)
        self.edits_flush_requested.emit()
        self.finish_edit(commit=True)
        document = self._document
        ancestors = set()
        for key in ids:
            parent = self._node_parents.get(key)
            while parent is not None:
                ancestors.add(parent)
                parent = self._node_parents.get(parent)
        collapsed = [key for key in ancestors if not document.nodes[key].expanded]
        if collapsed and not self.editable:
            raise MindMapError("只读模式不能展开隐藏节点，请先退出只读模式")
        focus = self.focus_id
        outside_focus = bool(focus) and any(key not in set(document.descendants(focus)) for key in ids)
        if collapsed and not self._can_incremental():
            candidate = self.engine.document
            for key in collapsed:
                candidate.nodes[key].expanded = True
            self.validate_document(candidate)
        if collapsed:
            with self.engine.transaction("大纲定位展开"):
                for key in collapsed:
                    self.engine.set_expanded(key, True)
        if focus and outside_focus:
            self.focus_id = None
            self.refresh()
        self.select_nodes(ids)
        if ids:
            bounds = self.node_items[ids[0]].sceneBoundingRect()
            for key in ids[1:]:
                bounds = bounds.united(self.node_items[key].sceneBoundingRect())
            self.view.ensureVisible(bounds, 35, 35)

    def call(self, callback):
        """界面动作的异常边界：通过错误信号报告失败，不弹出阻塞宿主的对话框。"""
        try:
            return callback()
        except (MindMapError, OSError, ValueError, KeyError) as exc:
            self.error_occurred.emit(str(exc))
            return None

    def register_action(
        self, action_id: str, text: str, callback, *, shortcut=None, mutating=True
    ) -> QAction:
        """注册扩展动作，callback 接收本组件；返回 QAction 可加入宿主菜单或工具栏。"""
        if action_id in self._actions:
            raise MindMapError(f"动作名称重复：{action_id}")
        action = QAction(text, self)
        action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        if shortcut:
            action.setShortcuts(
                [QKeySequence(value) for value in shortcut]
                if isinstance(shortcut, list)
                else [QKeySequence(shortcut)]
            )
        action.triggered.connect(lambda checked=False: self.call(lambda: callback(self)))
        self.addAction(action)
        self._actions[action_id] = action
        self._custom_actions.append(action_id)
        if mutating:
            self._mutating.add(action_id)
        action.setEnabled(not mutating or self.editable)
        return action

    def action(self, action_id: str) -> QAction:
        """取得内置或扩展动作，宿主可直接复用其启用状态和快捷键。"""
        return self._actions[action_id]

    def set_properties_handler(self, handler: Callable[[], None] | None) -> None:
        """由宿主接管属性入口；传入 None 恢复独立组件原有的属性弹窗。

        接管后属性动作属于查看入口，只读及原地编辑期间仍可打开侧栏；宿主负责
        结束原地编辑、同步选择及约束写入。这里不改变其他节点操作的快捷键范围。
        """
        if handler is not None and not callable(handler):
            raise MindMapError("属性处理器必须可调用或为 None")
        self._properties_handler = handler
        self.action("properties").setText("节点属性" if handler else "属性…")
        self._update_actions()

    def set_shortcuts(self, mapping: dict[str, str | list[str]]) -> None:
        """按动作标识覆盖快捷键；传入空列表可禁用对应快捷键。"""
        for key, values in mapping.items():
            if key not in self._actions:
                raise MindMapError(f"动作不存在：{key}")
            self._actions[key].setShortcuts(
                [QKeySequence(value) for value in ([values] if isinstance(values, str) else values)]
            )

    @property
    def wheel_mode(self) -> str:
        """普通滚轮当前行为：scroll 上下滚动，zoom 围绕鼠标缩放，默认 scroll。

        这是组件实例的视图偏好，不属于导图文档。宿主需要持久化时应监听
        wheel_mode_changed 信号保存，并在创建组件后调用 set_wheel_mode 恢复。
        """
        return self._wheel_mode

    def set_wheel_mode(self, mode: str) -> None:
        """切换普通滚轮行为并同步动作及全局设置；Ctrl＋滚轮始终缩放。

        仅接受 scroll 和 zoom，非法参数抛出 MindMapError 且保持原状态。实际变化
        才发送 wheel_mode_changed(str)，不修改引擎文档、脏标记或撤销历史；只读和
        文字编辑期间均可调用，组件自身不读写宿主应用的持久化设置。
        """
        if mode not in ("scroll", "zoom"):
            raise MindMapError("滚轮模式必须为 scroll 或 zoom")
        if mode == self._wheel_mode:
            return
        self._wheel_mode = mode
        self.view._wheel_scroll_remainder = 0.0
        self._sync_wheel_actions()
        self.wheel_mode_changed.emit(mode)

    def _sync_wheel_actions(self):
        """由组件偏好统一刷新菜单勾选、图标和中文提示，避免界面状态反向漂移。"""
        for mode in ("scroll", "zoom"):
            option = self._actions[f"wheel_{mode}"]
            option.setChecked(mode == self._wheel_mode)
            set_action_icon(option, f"wheel_{mode}", (self._ui_colors_override or self._document.theme))
        action = self._actions["wheel_mode"]
        label = self._actions[f"wheel_{self._wheel_mode}"].text()
        action.setText(f"滚轮行为：{label}")
        action.setToolTip(f"{label}；点击切换滚轮行为，Ctrl＋滚轮始终缩放")
        set_action_icon(action, f"wheel_{self._wheel_mode}", (self._ui_colors_override or self._document.theme))

    @property
    def edraw_paste_options(self) -> tuple[str, bool]:
        """返回亿图单树粘贴的默认方式及保留原后代开关，初始值为 (append, True)。

        此值只属于当前组件，不进入导图文件或撤销历史。宿主可监听
        edraw_paste_options_changed(str, bool) 保存，再通过 setter 恢复跨重启偏好。
        每次亿图粘贴仍会询问用户，多根来源被限制为追加时不会覆盖单树偏好。
        """
        return self._edraw_paste_options

    def set_edraw_paste_options(self, mode: str, keep_children: bool) -> None:
        """设置亿图粘贴默认选项；非法值抛出 MindMapError，且不产生任何文档变化。

        mode 仅允许 append、replace，keep_children 必须是布尔值。实际改变时才
        发送信号，适合宿主 QSettings 持久化；恢复偏好不受只读或编辑状态限制。
        """
        if mode not in ("append", "replace") or not isinstance(keep_children, bool):
            raise MindMapError("亿图粘贴方式必须为 append 或 replace，保留后代选项必须为布尔值")
        options = (mode, keep_children)
        if options != self._edraw_paste_options:
            self._edraw_paste_options = options
            self.edraw_paste_options_changed.emit(*options)

    def _create_actions(self):
        definitions = [
            ("add_child", "添加子节点", "Tab", lambda w: w.add_child(), True),
            ("add_sibling", "添加同级节点", "Return", lambda w: w.add_sibling(), True),
            ("insert_parent", "插入父节点", "Shift+Return", lambda w: w.insert_parent(), True),
            ("edit", "编辑文字", "F2", lambda w: w.edit_selected(), True),
            ("properties", "属性…", "Alt+Return", lambda w: w.edit_properties(), True),
            ("delete", "删除", "Delete", lambda w: w.delete_selected(), True),
            ("copy", "复制", "Ctrl+C", lambda w: w.copy(), False),
            ("cut", "剪切", "Ctrl+X", lambda w: w.cut(), True),
            ("paste", "粘贴", "Ctrl+V", lambda w: w.paste(), True),
            ("undo", "撤销", "Ctrl+Z", lambda w: w.engine.undo(), True),
            ("redo", "重做", ["Ctrl+Y", "Ctrl+Shift+Z"], lambda w: w.engine.redo(), True),
            ("move_up", "上移", "Ctrl+Up", lambda w: w.engine.move_up(w.current_node_id()), True),
            (
                "move_down",
                "下移",
                "Ctrl+Down",
                lambda w: w.engine.move_up(w.current_node_id(), down=True),
                True,
            ),
            ("fold", "折叠 / 展开", "Space", lambda w: w.toggle_expanded(), True),
            (
                "expand_all",
                "展开全部",
                None,
                lambda w: w.engine.set_expanded(w.engine.root_id, True, recursive=True),
                True,
            ),
            (
                "collapse_all",
                "折叠所选分支",
                None,
                lambda w: w.engine.set_expanded(w.current_node_id(), False, recursive=True),
                True,
            ),
            ("image", "插入本地图片…", None, lambda w: w.choose_image(), True),
            ("image_url", "从网址导入图片…", None, lambda w: w.choose_image_url(), True),
            (
                "remove_image",
                "移除图片",
                None,
                lambda w: w.engine.update_node(w.current_node_id(), image_id=None),
                True,
            ),
            ("hyperlink", "打开超链接", None, lambda w: w.open_hyperlink(), False),
            ("focus", "聚焦此分支", "Ctrl+F", lambda w: w.focus_node(w.current_node_id()), False),
            ("unfocus", "返回完整导图", "Escape", lambda w: w.focus_node(None), False),
            ("zoom_in", "放大", "Ctrl+=", lambda w: w.view.zoom(1.2), False),
            ("zoom_out", "缩小", "Ctrl+-", lambda w: w.view.zoom(1 / 1.2), False),
            ("fit", "适应窗口", "Ctrl+0", lambda w: w.fit_to_view(), False),
            ("center", "回到中心", "Home", lambda w: w.center_root(), False),
            ("wheel_mode", "滚轮行为", None, lambda w: None, False),
            ("wheel_scroll", "滚轮上下滚动", None, lambda w: w.set_wheel_mode("scroll"), False),
            ("wheel_zoom", "滚轮缩放", None, lambda w: w.set_wheel_mode("zoom"), False),
            ("branch_style", "分支连接线样式", None, lambda w: None, True),
        ]
        for key, label, shortcut, callback, mutating in definitions:
            self.register_action(key, label, callback, shortcut=shortcut, mutating=mutating)
        self._custom_actions.clear()
        # 默认工具栏只保留历史和视图操作；添加子节点、编辑文字、节点属性及分支样式
        # 仍注册为组件动作，供快捷键、右键菜单和宿主菜单复用。未加入工具栏的动作
        # 不会进入其溢出菜单，也不会留下占位按钮；属性与分支由属性面板继续提供。
        for key in ("undo", "redo", "fit", "center"):
            # 历史操作从工具栏起始处排列，仅在视图操作前分隔，避免开头留下空白占位。
            if key == "fit":
                self.toolbar.addSeparator()
            add_icon_action(self.toolbar, self._actions[key], key)
        # 滚轮设置移至工作区右上角菜单；保留动作和菜单，独立画布与宿主仍可直接复用。
        wheel_action = self._actions["wheel_mode"]
        self.wheel_menu = IconOptionMenu(self.toolbar)
        self.wheel_menu.setAccessibleName("滚轮行为选择")
        self._wheel_action_group = QActionGroup(self)
        self._wheel_action_group.setExclusive(True)
        for mode in ("scroll", "zoom"):
            action = self._actions[f"wheel_{mode}"]
            action.setCheckable(True)
            self._wheel_action_group.addAction(action)
            # 展示动作只负责承载图标按钮，原动作继续管理模式、选中状态和宿主集成。
            # 因此无需清空动作中文名称，状态栏、悬停提示和无障碍信息仍能正常使用。
            button = IconMenuOptionButton(self.wheel_menu)
            button.setDefaultAction(action)
            button.setAccessibleName(action.text())
            button.setAccessibleDescription(action.toolTip())
            menu_action = QWidgetAction(self.wheel_menu)
            menu_action.setText(action.text())
            menu_action.setDefaultWidget(button)
            menu_action.triggered.connect(action.trigger)
            action.changed.connect(
                lambda original=action, display=menu_action: display.setEnabled(original.isEnabled())
            )
            self.wheel_menu.addAction(menu_action)
            action.triggered.connect(self.wheel_menu.close)
        wheel_action.setMenu(self.wheel_menu)
        self._sync_wheel_actions()
        # 分支动作及其四列菜单继续供宿主复用，但不创建默认工具栏按钮。
        # 保留菜单的预览、作用范围和提交连接，已有组件调用无需迁移。
        self.branch_menu = BranchStyleMenu(self.toolbar)
        self.branch_menu.aboutToShow.connect(self._prepare_branch_menu)
        self.branch_menu.preview_requested.connect(
            lambda kind: self.call(lambda: self.preview_branch_style(kind, scope=self._branch_scope))
        )
        self.branch_menu.preview_finished.connect(self.clear_branch_preview)
        self.branch_menu.scope_changed.connect(self._set_branch_scope)
        self.branch_menu.style_selected.connect(
            lambda kind: self.call(lambda: self._commit_branch_menu_style(kind))
        )
        branch_action = self._actions["branch_style"]
        branch_action.setMenu(self.branch_menu)
        self._sync_branch_actions()

    def _update_actions(self):
        editing = self.editor is not None
        for key, action in self._actions.items():
            action.setEnabled(
                key in ("wheel_mode", "wheel_scroll", "wheel_zoom")
                or (not editing and (self.editable or key not in self._mutating))
            )
        self._actions["undo"].setEnabled(self.editable and not editing and self.engine.can_undo)
        self._actions["redo"].setEnabled(self.editable and not editing and self.engine.can_redo)
        if self._properties_handler is not None:
            self._actions["properties"].setEnabled(True)
        self._actions["branch_style"].setEnabled(
            self.editable
            and not editing
            and self.layout_registry.info(self._document.layout.algorithm_id).supports_branch_styles
        )
        self._sync_branch_actions()

    def set_editable(self, editable: bool) -> None:
        """切换只读模式，取消尚未提交的文本编辑与下载后续写入。"""
        changed = self.editable != bool(editable)
        if not editable:
            self._cancel_branch_menu()
            self.finish_edit(commit=False)
        self.editable = editable
        self._update_actions()
        self.view.cancel_interaction()
        if changed:
            self.editable_changed.emit(bool(editable))

    def _ensure_editable(self):
        if not self.editable:
            raise MindMapError("当前画布为只读模式")

    def selected_node_ids(self) -> list[str]:
        """按树的先序顺序返回选择，移动后仍与文档层级一致。"""
        selected = {item.node.id for item in self.scene.selectedItems() if isinstance(item, NodeItem)}
        if len(selected) < 2:
            return list(selected)
        if self._incremental_layout is not None:
            return sorted(selected, key=self._incremental_layout.geometry.order)
        if self._node_order is None:
            self._node_order = {
                key: index for index, key in enumerate(self._document.descendants(self._document.root_id))
            }
        return sorted(selected, key=self._node_order.__getitem__)

    def current_node_id(self) -> str:
        """取得首个选中节点；无选择时使用聚焦节点，再回退到文档根节点。

        仅查询界面状态，不强制展开节点、不改变选择，也不创建历史记录。
        """
        ids = self.selected_node_ids()
        return ids[0] if ids else self.focus_id or self.engine.root_id

    def select_nodes(self, node_ids) -> None:
        """替换节点选择；折叠或聚焦之外的节点不会强制展开。"""
        self.view.cancel_interaction()
        self.scene.blockSignals(True)
        try:
            self.scene.clearSelection()
            for key in node_ids:
                self._ensure_node_item(key)
                if key in self.node_items:
                    self.node_items[key].setSelected(True)
        finally:
            self.scene.blockSignals(False)
        self._selection_changed()
        if self._virtualized:
            self._sync_viewport(force=True)

    def _selection_changed(self):
        if self._refreshing or self._disposed:
            return
        self._cancel_branch_menu()
        self._update_actions()
        self.view.update_cursor()
        self.selection_changed.emit(self.selected_node_ids())

    def _engine_changed(self, event):
        if self._disposed:
            return
        # 在画布刷新引发选择信号之前通知宿主，防止旧侧栏输入被提交到新文档或撤销状态。
        if event.kind != "saved":
            self.view.node_drag.document_changed()
            # 自动保存只更新保存标记，不触发文档替换通知，不打断菜单或未完成输入。
            self.document_changing.emit(event)
            self._cancel_branch_menu()
            before = self.selected_node_ids()
            old_parents = {key: self._node_parents.get(key) for key in before}
            if self.editor is not None:
                self.finish_edit(commit=False)
            changes = event.changes
            if changes is not None:
                index = 1 if event.forward else 0
                delta = changes.collections.get("nodes", {})
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
                changes.apply(self._document, forward=event.forward)
                for key, pair in delta.items():
                    node = pair[index]
                    old = pair[1 - index]
                    if node is not None and (old is None or old.children != node.children):
                        for child in node.children:
                            self._node_parents[child] = key
                self._measure_dirty = None if "theme" in changes.settings else event.node_ids
            else:
                self._document = self.engine.document
                self._node_parents = self._document.parents()
                new_ids = []
                self._measure_dirty = None
                self._incremental_layout = None
            self._node_order = None
            after = [key for key in before if key in self._document.nodes]
            parents = self._node_parents
            if new_ids:
                new_set = set(new_ids)
                after = [
                    key for key in new_ids if parents.get(key) not in new_set
                ]
            if not after:
                after = [old_parents[key] for key in before if old_parents.get(key) in self._document.nodes]
                after = after or [self.engine.root_id]
            if event.kind == "reset":
                self.focus_id = None
                self._selection_undo.clear()
                self._selection_redo.clear()
                after = [self.engine.root_id]
            elif event.kind == "undo" and self._selection_undo:
                selection = self._selection_undo.pop()
                self._selection_redo.append(selection)
                after = selection[0]
            elif event.kind == "redo" and self._selection_redo:
                selection = self._selection_redo.pop()
                self._selection_undo.append(selection)
                after = selection[1]
            elif event.kind == "change":
                self._selection_undo.append((before, after))
                del self._selection_undo[: -self.engine.history_limit]
                self._selection_redo.clear()
            if self.focus_id not in self._document.nodes:
                self.focus_id = None
            self.refresh()
            self.select_nodes(after)
        self._update_actions()
        self.history_changed.emit(self.engine.can_undo, self.engine.can_redo, self.engine.is_dirty)
        self.document_changed.emit(event)

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
        dirty = self._measure_dirty
        if dirty is None or self._visual_document is None:
            visuals, sizes = self.factory.measure(document)
        else:
            visuals, sizes = self._visuals, self._measurements
            self.factory.update_measurements(document, dirty, visuals, sizes)
        self._visual_document, self._visuals = document, visuals
        self._measure_dirty = ()
        self._measurements = sizes
        if self._can_incremental():
            key = (id(self.layout_registry.get(document.layout.algorithm_id)),
                   document.root_id, repr(document.layout))
            if self._incremental_layout is None or key != self._incremental_key or dirty is None:
                self.layout_registry.validate_config(document.layout)
                self._incremental_layout = IncrementalTreeLayout(document, sizes)
                self._incremental_key = key
            else:
                self._incremental_layout.update(document, sizes, dirty)
            self.layout_result = self._incremental_layout.result()
        else:
            self._incremental_layout = None
            self.layout_result = self.layout_registry.layout(document, sizes, focus_id=self.focus_id)
        result = self.layout_result
        self._virtualized = self._incremental_layout is not None and len(result.nodes) > VIRTUALIZATION_NODE_LIMIT
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

    def _can_incremental(self):
        """仅启用受控内置树算法；自定义算法、聚焦及宿主替换的布局入口继续完整校验。"""
        algorithm = self.layout_registry.get(self._document.layout.algorithm_id)
        return (self.focus_id is None and type(algorithm) is TreeLayout
                and "layout" not in self.layout_registry.__dict__ and "layout" not in algorithm.__dict__)

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

    def _viewport_changed(self):
        """视图平移、缩放或尺寸变化后同步屏内对象；构造及批量刷新期间由 refresh 统一调用。"""
        if getattr(self, "_virtualized", False) and not self._refreshing and not self._disposed:
            self._sync_viewport()

    def _sync_viewport(self, *, force=False):
        """保留选择、编辑和鼠标捕获对象，其余图元仅维护视口及预取边距。

        连接线采用父级局部路径和世界原点。子树平移时只移动现有图元，保持 Qt
        路径和画笔不变；视口外实体仍存在于独立读模型和按需布局映射中。
        """
        selected = {item.node.id for item in self.scene.selectedItems() if isinstance(item, NodeItem)}
        if self.editor is not None:
            selected.add(self.editor.node_id)
        grabbed = self.scene.mouseGrabberItem()
        if isinstance(grabbed, NodeItem):
            selected.add(grabbed.node.id)
        box = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        signature = (id(self.layout_result), box.x(), box.y(), box.width(), box.height(), frozenset(selected))
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
            item = self.node_items.get(key)
            if item is None:
                item = NodeItem(document.nodes[key], visuals[key], document.theme, self)
                self.scene.addItem(item)
                self.node_items[key] = item
            elif (
                item.visual is not visuals[key]
                or item.node.expanded != document.nodes[key].expanded
                or item.node.note != document.nodes[key].note
                or item.node.hyperlink != document.nodes[key].hyperlink
                or item.theme.accent != document.theme.accent
            ):
                item.update_content(document.nodes[key], visuals[key], document.theme)
            else:
                # 视觉未变也同步实体引用，正文导航等读取当前快照，但不使 Qt 几何失效。
                item.node, item.theme = document.nodes[key], document.theme
            item.setPos(rect.x, rect.y)

    def set_ui_colors(self, colors: UiColors | None) -> None:
        """覆盖本组件的界面色板，不修改导图主题、场景内容、选择和撤销历史。

        None 恢复原有跟随导图主题的行为。只刷新控件外观，保留原地编辑器、
        属性弹层、鼠标位置和画布倍率；错误类型抛出 TypeError。
        """
        if colors is not None and not isinstance(colors, UiColors):
            raise TypeError("界面色板必须为 UiColors 或 None")
        self._ui_colors_override = colors
        self._apply_ui_theme()

    def _apply_ui_theme(self):
        theme = self._ui_colors_override or self._document.theme
        colors = ui_colors(theme)
        # 嵌入模式只覆盖屏幕底色；导出重新从文档构建场景，仍使用文档背景。
        # 每次文档刷新也同步此处，避免打开文件或撤销后闪回旧的内部卡片背景。
        background = colors.field if self._embedded else self._document.theme.background
        self.scene.setBackgroundBrush(QColor(background))
        self._view_card.set_background(background)
        if colors != self._ui_colors:
            self._ui_colors = colors
            # 仅在当前组件内提供菜单、网址输入等样式，不修改宿主的应用样式。
            self.setStyleSheet(
                f"QWidget#mindmap_canvas {{ background: {colors.shell}; }}" + dialog_styles(colors)
            )
            # QSS 重设会恢复旧调色板，因此在重设之后再写入新调色板，避免换成
            # 深色后透明圆角仍露出首次显示时的浅色底。
            apply_ui_palette(self, colors, window=colors.shell)
        apply_toolbar_theme(self.toolbar, theme)
        if self._view_controls is not None:
            self._view_controls.set_ui_colors(colors)
        # 菜单选项复用组件动作，并非工具栏的直接子动作，主题刷新时单独同步其图标。
        # 属性按钮移出工具栏后，编辑菜单和右键菜单仍沿用当前主题的属性图标。
        set_action_icon(self._actions["properties"], "properties", theme)
        self._sync_wheel_actions()
        self._sync_branch_actions()

    def _branch_selection(self) -> list[str]:
        return self.selected_node_ids() or [self.current_node_id()]

    def _sync_branch_actions(self) -> None:
        """按当前范围统计正式状态；同一主题下后代不同样式也显示为混合。"""
        kinds = self._branch_kinds(self._branch_selection(), self._branch_scope)
        self.branch_menu.sync(kinds, self._branch_scope, (self._ui_colors_override or self._document.theme))
        current = next(iter(kinds)) if len(kinds) == 1 else None
        label = "多种样式" if len(kinds) != 1 else current.label if current else "原始曲线"
        action = self._actions["branch_style"]
        action.setText(f"分支连接线：{label}")
        action.setToolTip(f"分支连接线：{label}；可设置整个子树或仅本级")
        icon = branch_style_icon(current, (self._ui_colors_override or self._document.theme))
        if action.icon().cacheKey() != icon.cacheKey():
            action.setIcon(icon)

    def _branch_kinds(self, node_ids, scope="subtree"):
        """画布和侧栏共用逻辑子树的样式摘要，折叠变化不再触发全图样式扫描。"""
        if self._incremental_layout is not None:
            return self._incremental_layout.style_kinds(node_ids, scope)
        ids = branch_style_targets(self._document, node_ids, scope=scope)
        return {get_branch_style(self._document.nodes[key].style) for key in ids}

    def _set_branch_scope(self, scope: str) -> None:
        """作用范围是组件偏好，不进入导图文件和撤销历史。"""
        if scope not in ("level", "subtree"):
            raise MindMapError("分支样式作用范围必须为 level 或 subtree")
        self.clear_branch_preview()
        changed = self._branch_scope != scope
        self._branch_scope = scope
        self._sync_branch_actions()
        if changed:
            self.branch_scope_changed.emit(scope)

    def _prepare_branch_menu(self) -> None:
        self.clear_branch_preview()
        self._sync_branch_actions()

    def _commit_branch_menu_style(self, kind) -> None:
        """成功、无变化和被钩子拒绝的操作都恢复正式勾选，避免按钮自行翻转残留。"""
        try:
            self.set_branch_style(kind, scope=self._branch_scope)
        finally:
            self._cancel_branch_menu()
            self._sync_branch_actions()

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

    def _show_branch_layout(self, result) -> None:
        center = self.view.mapToScene(self.view.viewport().rect()).boundingRect().center()
        self._set_branch_items(result)
        bounds = result.bounds
        self.scene.setSceneRect(QRectF(bounds.x, bounds.y, bounds.width, bounds.height))
        self.view.update_navigation_rect(center=center)

    def clear_branch_preview(self) -> None:
        """恢复最后一次提交的几何；重复调用安全，不写文档也不发送历史事件。"""
        if self._preview_layout is not None:
            self._preview_layout = None
            if not self._disposed:
                self._show_branch_layout(self.layout_result)

    def _cancel_branch_menu(self, *, restore=True) -> None:
        if restore:
            self.clear_branch_preview()
        else:
            self._preview_layout = None
        if hasattr(self, "branch_menu"):
            self.branch_menu.close()

    def _ensure_branch_editable(self) -> None:
        self._ensure_editable()
        if self.editor is not None:
            raise MindMapError("请先结束文字编辑再设置分支样式")
        if not self.layout_registry.info(self.engine.document.layout.algorithm_id).supports_branch_styles:
            raise MindMapError("当前布局算法不支持分支连接线样式")

    def preview_branch_style(self, kind: BranchStyle | str | None, *, scope="subtree") -> None:
        """在独立候选文档上预览，只借用节点尺寸，不修改正式文档和历史。

        已关闭/禁用组件可能仍收到离开或聚焦事件；这些迟到事件直接丢弃。
        导出始终读取引擎快照，因此预览不会泄漏到 PNG、SVG、PDF 或保存文件。
        """
        if self._disposed or not self._actions["branch_style"].isEnabled():
            return
        candidate = self.engine.document
        apply_branch_style(candidate, self._branch_selection(), kind, scope=scope)
        result = self.layout_registry.layout(candidate, self._measurements, focus_id=self.focus_id)
        self._preview_layout = result
        self._show_branch_layout(result)

    def set_branch_style(self, kind: BranchStyle | str | None, *, scope: str = "subtree") -> None:
        """先试算再一次提交发出的连接线设置，失败不改变文档、历史与选择。

        kind 接受枚举、语义名称或 None（恢复旧原始曲线）。默认 subtree 显式更新
        全部后代，level 仅改所选主题；不使用跨层隐式继承。优先采用当前多选，
        没有选择时采用聚焦主题，否则采用根主题；文字编辑、只读与不支持的布局拒绝操作。
        """
        self._ensure_branch_editable()
        ids = self._branch_selection()
        candidate = self.engine.document
        apply_branch_style(candidate, ids, kind, scope=scope)
        self.layout_registry.layout(candidate, self._measurements, focus_id=self.focus_id)
        self._cancel_branch_menu()
        self.engine.set_branch_style(ids, kind, scope=scope)
        self._sync_branch_actions()

    def reset_view(self) -> None:
        """请求以 100% 原始大小显示，并将文档根节点放在可见画布中央。

        构造画布、新建和成功打开文档后调用；实际定位延迟到 Qt 完成显示与布局。
        隐藏标签保留请求，首次显示时执行；连续请求只处理最新文档，不使用隐藏
        画布的占位尺寸。定位完成后，窗口调整和标签切换不再重置用户视图。
        此接口仅调整视图，不提交输入，不修改文档、选择、折叠状态或撤销历史。
        已释放的画布忽略请求；手动居中且保留倍率时使用 center_root()。
        """
        if self._disposed:
            return
        self._view_reset_pending = True
        self._schedule_view_reset()

    def _schedule_view_reset(self) -> None:
        """仅在可见视口有待处理请求时排程；显示和尺寸事件可安全重复调用。

        重启同一个零间隔定时器，使本轮 Qt 布局先完成；隐藏时不循环排程，等待
        后续显示事件再次唤醒。已初始化的视图不会因正常重排或刷新重新定位。
        """
        if (not self._disposed and self._view_reset_pending and self.view.isVisible()
                and not self.view.viewport().size().isEmpty()):
            self._view_reset_timer.start(0)

    def _apply_view_reset(self) -> None:
        """在有效布局上消费一次初始化请求；隐藏或释放后的迟到回调不触碰视图。"""
        if (self._disposed or not self._view_reset_pending or not self.view.isVisible()
                or self.view.viewport().size().isEmpty() or self._refreshing):
            return
        root = self.node_items.get(self.engine.root_id)
        if root is None or root.sceneBoundingRect().isEmpty():
            return
        # 先消费请求，再恢复倍率，防止变换引发的尺寸事件重复排程。导航余量必须
        # 按恢复后的 100% 倍率计算，定位目标使用根节点中心而非整张导图的中心。
        self._view_reset_pending = False
        self.view.resetTransform()
        self.view.update_navigation_rect(center=root.sceneBoundingRect().center())
        self.view.update_cursor()

    def center_root(self):
        """将根节点或当前聚焦节点置于视口中央，保持用户的缩放比例。"""
        key = self.focus_id or self.engine.root_id
        rect = self.layout_result.nodes.get(key)
        if not self._disposed and rect is not None:
            self.view.update_navigation_rect(center=QRectF(rect.x, rect.y, rect.width, rect.height).center())

    def fit_to_view(self):
        """按可见内容适应窗口，将最终倍率限制在 10%～200%，不计入导航余量。

        先提交正在编辑的节点，再按更新后的布局计算。达到最小倍率时保留内容
        中心居中，允许超大导图的边缘超出视口，由用户平移查看，不继续缩小。
        按钮、快捷键和分支聚焦均复用此入口，与交互缩放共用同一最小倍率。
        """
        self.finish_edit(commit=True)
        rect = self.layout_result.bounds
        content = QRectF(rect.x, rect.y, rect.width, rect.height)
        self.view.fitInView(content, Qt.AspectRatioMode.KeepAspectRatio)
        # Qt 的自动适配不会经过 MindMapView.zoom，必须在此显式约束最终倍率。
        # 超出上下限时重建等比变换，避免沿用不受限制的倍率或累计缩放误差。
        fitted = self.view.transform().m11()
        target = min(2.0, max(MIN_ZOOM, fitted))
        if target != fitted:
            self.view.resetTransform()
            self.view.scale(target, target)
        # 按最终倍率重新换算一屏导航余量。触及任一倍率限制后仍居中内容范围，
        # 不能改为居中根节点，否则单向布局会偏向一端，重复适配也无法正确定位。
        self.view.update_navigation_rect(center=content.center())

    def focus_node(self, node_id: str | None):
        """临时聚焦分支，恢复时不会改动原文档的折叠状态。"""
        self.finish_edit(commit=True)
        if node_id is not None:
            self.engine.get_node(node_id)
        self.focus_id = node_id
        self.refresh()
        self.select_nodes([node_id or self.engine.root_id])
        self.fit_to_view()

    def begin_edit(self, node_id: str):
        """开始原地编辑；一个编辑会话在提交时只生成一次引擎操作。"""
        if not self.editable or node_id not in self.layout_result.nodes:
            return
        self.finish_edit(commit=True)
        self.select_nodes([node_id])
        item = self.node_items[node_id]
        item.editing = True
        item.update()
        self.editor = NodeEditor(self, self.engine.get_node(node_id), item.visual)
        self.editor.setPos(item.pos() + item.visual.text_position)
        self.scene.addItem(self.editor)
        self.editing_changed.emit(True)
        self.view.setFocus()
        self.editor.setFocus(Qt.FocusReason.OtherFocusReason)
        self._update_actions()
        self.view.update_cursor()

    def finish_edit(self, *, commit=True):
        """提交或取消当前编辑。删除临时编辑器后再通知引擎，避免刷新重入。"""
        editor = self.editor
        if editor is None:
            return
        editor.finishing = True
        node_id, text = editor.node_id, editor.toPlainText()
        self.editor = None
        self.scene.removeItem(editor)
        editor.deleteLater()
        if node_id in self.node_items:
            self.node_items[node_id].editing = False
            self.node_items[node_id].update()
        if commit and self.editable:
            self.call(lambda: self.engine.update_node(node_id, text=text))
        self._update_actions()
        self.view.setFocus()
        self.view.update_cursor()
        self.editing_changed.emit(False)

    def edit_selected(self):
        """编辑当前节点的正文；具体只读与输入法处理由节点编辑器负责。"""
        self.begin_edit(self.current_node_id())

    def add_child(self):
        """为当前节点添加子节点并开始编辑正文，返回新节点标识。

        只读时拒绝操作；节点属性及分支继承遵循引擎规则，正文通过独立编辑会话提交。
        """
        self._ensure_editable()
        key = self.engine.add_child(self.current_node_id())
        self.begin_edit(key)
        return key

    def add_sibling(self):
        """在当前节点旁添加同级主题并开始正文编辑，返回新节点标识。

        同级顺序、方向继承及根节点限制统一由引擎校验，失败不保留部分结构修改。
        """
        self._ensure_editable()
        key = self.engine.add_sibling(self.current_node_id())
        self.begin_edit(key)
        return key

    def insert_parent(self):
        """为当前节点插入父主题并开始编辑，返回新增主题标识。

        只读、非法根操作或引擎钩子拒绝时抛出异常，不绕过引擎的事务及撤销边界。
        """
        self._ensure_editable()
        key = self.engine.insert_parent(self.current_node_id())
        self.begin_edit(key)
        return key

    def delete_selected(self):
        """一次删除选择的节点子树，节点和图片资源共用一条撤销记录。"""
        self._ensure_editable()
        ids = self.selected_node_ids()
        if ids:
            self.engine.remove_nodes(ids)

    def toggle_expanded(self):
        """切换当前节点的文档折叠状态，通过引擎提交，支持撤销、重做及自动保存。"""
        self._ensure_editable()
        node = self.engine.get_node(self.current_node_id())
        self.engine.set_expanded(node.id, not node.expanded)

    def copy(self):
        """使用注入的 JSON 与 Markdown 格式导出片段，文件与剪贴板共享解析协议。"""
        fragment = self.engine.copy_fragment(self.selected_node_ids())
        mime = QMimeData()
        mime.setData(
            CLIPBOARD_MIME, encode_document(fragment, registry=self.format_registry, format_id="json")
        )
        mime.setText(
            encode_document(fragment, registry=self.format_registry, format_id="markdown").decode("utf-8")
        )
        QApplication.clipboard().setMimeData(mime)

    def cut(self):
        """先按注入格式复制当前选择，再删除相应子树；只读或选中根节点时拒绝。

        剪贴板编码失败时不会执行删除；正式删除使用引擎的历史与资源清理规则。
        """
        self._ensure_editable()
        if self.engine.root_id in self.selected_node_ids():
            raise MindMapError("不能剪切根节点")
        self.copy()
        self.engine.remove_nodes(self.selected_node_ids())

    def paste(self):
        """通过注入格式完整解析剪贴板，失败时不改变选择、文档或撤销历史。"""
        self._ensure_editable()
        mime = QApplication.clipboard().mimeData()
        if mime is None:
            return []
        if mime.hasFormat(CLIPBOARD_MIME):
            fragment = decode_document(bytes(mime.data(CLIPBOARD_MIME)), registry=self.format_registry)
        elif mime.hasFormat(EDRAW_CLIPBOARD_MIME):
            # 先读完并验证结构，再询问放置方式；亿图解析失败时不能降级为纯文本。
            fragment = read_edraw_clipboard(bytes(mime.data(EDRAW_CLIPBOARD_MIME)))
            return self._paste_edraw(fragment)
        elif mime.hasText():
            text = mime.text()
            first = next((line.strip() for line in text.splitlines() if line.strip()), "")
            document = decode_document(
                text.encode("utf-8"),
                registry=self.format_registry,
                format_id="markdown" if first.startswith(("#", "- ", "* ")) else "text",
            )
            fragment = MindMapDocument.new("剪贴板")
            fragment.root.children = [document.root_id]
            fragment.nodes.update(document.nodes)
        else:
            return []
        ids = self.engine.paste_fragment(self.current_node_id(), fragment)
        self.select_nodes(ids)
        return ids

    def _paste_edraw(self, fragment: MindMapDocument) -> list[str]:
        """询问亿图片段的放置方式，并原子地更新树、选择和成功后的默认偏好。"""
        target_id = self.current_node_id()
        target = self.engine.get_node(target_id)
        generation, revision = self.engine.document_generation, self.engine.revision
        roots = fragment.root.children
        mode, keep_children = self.edraw_paste_options
        choice = edraw_paste_options(
            self, target_text=target.text, root_count=len(roots), node_count=len(fragment.nodes) - 1,
            mode=mode, keep_children=keep_children,
            theme=self._ui_colors_override or self._document.theme,
        )
        if choice is None:
            return []
        self._ensure_editable()
        # 模态弹窗仍会处理定时器和宿主事件，禁止将确认前的目标套用到已替换的文档。
        if self._disposed or (generation, revision) != (
            self.engine.document_generation, self.engine.revision
        ):
            raise MindMapError("粘贴期间导图已发生变化，请重新粘贴")
        if choice.mode not in ("append", "replace") or not isinstance(choice.keep_children, bool):
            raise MindMapError("亿图粘贴选项无效")
        if choice.mode == "replace" and len(roots) != 1:
            raise MindMapError("多个并列主题只能追加到选中节点下")

        # 新主题沿用正常 add_child 的分支继承规则，字体、颜色、方向等保持项目默认值。
        # 每个节点分别深拷贝，避免之后编辑某条分支时连带改变其他新主题。
        if "branch" in target.style:
            for node in fragment.nodes.values():
                if node.id != fragment.root_id:
                    node.style["branch"] = deepcopy(target.style["branch"])
        with self.engine.transaction("粘贴亿图脑图"):
            if choice.mode == "replace":
                source = fragment.nodes[roots[0]]
                if not choice.keep_children:
                    self.engine.remove_nodes(target.children)
                self.engine.update_node(target_id, text=source.text, text_format="plain")
                # paste_fragment 忽略片段根；把来源根作为合成根即可只插入它的后代。
                # 目标 ID、同级位置和本项目属性始终保留；保留的旧后代排在新后代之前。
                replacement = deepcopy(fragment)
                del replacement.nodes[replacement.root_id]
                replacement.root_id = source.id
                self.engine.paste_fragment(target_id, replacement)
                ids = [target_id]
            else:
                ids = self.engine.paste_fragment(target_id, fragment)
        self.select_nodes(ids)
        # 引擎通知默认选中新插入的后代；替换后应选中目标，重做时也必须恢复同一选择。
        if self.engine.revision != revision and self._selection_undo:
            before, _ = self._selection_undo[-1]
            self._selection_undo[-1] = (before, list(ids))
        self.set_edraw_paste_options(choice.mode if len(roots) == 1 else mode, choice.keep_children)
        return ids

    def edit_properties(self):
        """打开当前节点属性入口；宿主接管时转交侧栏，否则使用共享属性弹窗。

        弹窗确认后先验证完整候选，再以正式事务提交；取消不修改文档和历史。
        """
        if self._properties_handler is not None:
            self._properties_handler()
            return
        self._ensure_editable()
        key = self.current_node_id()
        result = node_properties(
            self,
            self.engine.get_node(key),
            branch_styles=self.layout_registry.info(
                self._document.layout.algorithm_id
            ).supports_branch_styles,
            branch_scope=self._branch_scope,
            theme=self._document.theme,
            root_id=self._document.root_id,
            parents=self._document.parents(),
        )
        if result is not None:
            candidate = self.engine.document
            for name, value in result.patch.items():
                setattr(candidate.nodes[key], name, deepcopy(value))
            if result.branch_changed:
                apply_branch_style(candidate, [key], result.branch_kind, scope=result.branch_scope)
            self.validate_document(candidate)
            # 属性和子树样式在同一事务中提交；只改字体时不会重设后代已有的连线。
            with self.engine.transaction("修改节点属性"):
                self.engine.update_node(key, **result.patch)
                if result.branch_changed:
                    self.engine.set_branch_style([key], result.branch_kind, scope=result.branch_scope)
            self._branch_scope = result.branch_scope
            self._sync_branch_actions()

    def import_image(self, node_id: str, path: str | Path):
        """导入本地图片，先验证可解码再修改文档。"""
        self._ensure_editable()
        data = Path(path).read_bytes()
        _, media_type = decode_image(data)
        self.engine.attach_image(node_id, data, media_type)
        self.image_imported.emit(node_id)

    def import_image_url(self, node_id: str, url: str) -> QNetworkReply:
        """异步下载图片；文档切换、节点删除或进入只读后丢弃过期结果。"""
        self._ensure_editable()
        self.engine.get_node(node_id)
        address = QUrl(url)
        if address.scheme() not in ("http", "https") or not address.host():
            raise MindMapError("图片网址必须是有效的 HTTP 或 HTTPS 地址")
        generation = self.engine.document_generation
        request = QNetworkRequest(address)
        request.setTransferTimeout(30_000)
        reply = self.network.get(request)
        self._downloads.add(reply)
        reply.downloadProgress.connect(
            lambda received, total: (
                reply.abort() if received > 25 * 1024 * 1024 or total > 25 * 1024 * 1024 else None
            )
        )

        def finished():
            self._downloads.discard(reply)
            try:
                if self._disposed or not self.editable or generation != self.engine.document_generation:
                    return
                if reply.error() != QNetworkReply.NetworkError.NoError:
                    raise MindMapError(f"图片下载失败：{reply.errorString()}")
                data = bytes(reply.readAll())
                _, media_type = decode_image(data)
                self.engine.attach_image(node_id, data, media_type)
                self.image_imported.emit(node_id)
            except (MindMapError, OSError) as exc:
                self.error_occurred.emit(str(exc))
            finally:
                reply.deleteLater()

        reply.finished.connect(finished)
        return reply

    def choose_image(self):
        """为当前节点打开本地图片选择器；取消保持原状，实际导入复用图片校验接口。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", "图片 (*.png *.jpg *.jpeg *.webp *.gif *.svg)"
        )
        if path:
            self.import_image(self.current_node_id(), path)

    def choose_image_url(self):
        """询问当前节点的图片网址并启动异步导入；取消不创建下载或修改文档。"""
        url, accepted = QInputDialog.getText(self, "导入网络图片", "图片网址")
        if accepted and url:
            self.import_image_url(self.current_node_id(), url)

    def open_hyperlink(self):
        """通过系统桌面服务打开当前节点链接；遵循已有地址校验，不改变文档及历史。"""
        address = QUrl(self.engine.get_node(self.current_node_id()).hyperlink)
        if address.scheme() not in ("http", "https", "mailto"):
            raise MindMapError("超链接支持 http、https 和 mailto 协议")
        if not QDesktopServices.openUrl(address):
            raise MindMapError("无法打开超链接")

    def export(self, path: str | Path, *, expand_all=False, scale=1.0):
        """导出正式文档；允许宿主提交侧栏输入，不触碰画布原地编辑器中的临时文字。"""
        # 导出读取正式文档；宿主可在快照建立前提交侧栏内已完成的有效输入。
        self.edits_flush_requested.emit()
        export_document(
            self.engine.document,
            path,
            layout_registry=self.layout_registry,
            expand_all=expand_all,
            scale=scale,
        )

    def set_layout(self, config: LayoutConfig) -> None:
        """在提交前试算新布局，缺失算法、非法参数或计算失败均不产生部分修改。"""
        self._ensure_editable()
        candidate = self.engine.document
        candidate.layout = config
        self.validate_document(candidate)
        self.engine.set_layout(config)

    def validate_document(self, document: MindMapDocument) -> None:
        """替换前验证算法、图片、测量与布局结果，失败时当前引擎和画布不变。"""
        document.validate()
        self.layout_registry.validate_config(document.layout)
        _, sizes = self.factory.measure(document)
        self.layout_registry.layout(document, sizes)

    def context_menu(self, global_position):
        """在指定全局位置显示复用内置及扩展 QAction 的右键菜单。

        动作状态及快捷键归画布统一管理；查看菜单本身不提交文档修改，关闭后释放菜单。
        """
        menu = QMenu(self)
        groups = [
            ("add_child", "add_sibling", "insert_parent", "edit", "properties", "delete"),
            ("copy", "cut", "paste", "undo", "redo"),
            ("move_up", "move_down", "fold", "expand_all", "collapse_all"),
            ("image", "image_url", "remove_image", "hyperlink"),
            ("focus", "unfocus", "fit", "center"),
        ]
        for group in groups:
            if menu.actions():
                menu.addSeparator()
            for key in group:
                menu.addAction(self._actions[key])
        if self._custom_actions:
            menu.addSeparator()
            for key in self._custom_actions:
                menu.addAction(self._actions[key])
        menu.exec(global_position)
