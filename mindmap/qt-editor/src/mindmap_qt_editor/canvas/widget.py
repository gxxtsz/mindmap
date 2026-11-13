"""可嵌入宿主窗口的完整思维导图组件。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

from mindmap_core import (
    BranchStyle,
    FormatRegistry,
    LayoutConfig,
    LayoutRegistry,
    MindMapDocument,
    MindMapEngine,
    MindMapError,
    MindMapPageView,
    ValidatedDocument,
    apply_branch_style,
    branch_style_targets,
    get_branch_style,
)
from mindmap_core.formats import (
    create_default_format_registry,
)
from mindmap_core.layouts import (
    create_default_layout_registry,
    create_incremental_layout,
    supports_incremental_layout,
)
from mindmap_core.layouts.graph import build_graph_layout, graph_layout
from mindmap_core.node_pages import GRAPH_PAGE_TYPE
from mindmap_qt_render.exporting import export_document
from mindmap_qt_render.rendering import VisualFactory, decode_image
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QKeySequence
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QFileDialog,
    QGraphicsScene,
    QInputDialog,
    QMenu,
    QMessageBox,
    QToolBar,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)
from shiboken6 import isValid

from mindmap_qt_editor.controls.base import CardFrame
from mindmap_qt_editor.controls.branch_styles import BranchStyleMenu, branch_style_icon
from mindmap_qt_editor.controls.dialogs import node_properties
from mindmap_qt_editor.icons import (
    IconMenuOptionButton,
    IconOptionMenu,
    add_icon_action,
    apply_toolbar_theme,
    set_action_icon,
)
from mindmap_qt_editor.theme import UiColors, apply_ui_palette, dialog_styles, ui_colors

from .clipboard_actions import ClipboardActionsMixin
from .graph import GraphCanvasMixin
from .inline_editing import InlineEditingMixin
from .inspiration import InspirationCanvasMixin
from .interactions import document_interaction
from .navigation import PageNavigationMixin
from .scene_sync import SceneSyncMixin
from .selection import NodeSelectionMixin
from .view import MIN_ZOOM, MindMapView
from .view_controls import _ViewControls

_ENGINE_DOCUMENT = MindMapEngine.document


class MindMapWidget(
    SceneSyncMixin,
    InlineEditingMixin,
    ClipboardActionsMixin,
    GraphCanvasMixin,
    NodeSelectionMixin,
    InspirationCanvasMixin,
    PageNavigationMixin,
    QWidget,
):
    """原生画布组件。

    宿主先创建 QApplication，再将本组件加入任意布局。engine 可省略，此时创建
    独立引擎。只读模式约束组件发起的修改，宿主仍可通过引擎加载或更新文档。
    所有图形接口应在 GUI 线程使用，使用 dispose() 可主动解除订阅和网络请求。
    """

    selection_changed = Signal(list)
    document_changed = Signal(object)
    history_changed = Signal(bool, bool, bool)
    error_occurred = Signal(str)
    # 图片创建成功后在 GUI 线程同步通知，参数为新纯图片子节点 ID；失败不发出。
    # 节点及原始资源由引擎文档和历史管理，接收方通过 ID 查询，不拥有可写实体；
    # 非活动页下载完成也会通知，但信号本身不导航。画布释放后不再发出。
    image_imported = Signal(str)
    wheel_mode_changed = Signal(str)
    edraw_paste_options_changed = Signal(str, bool, bool)
    editable_changed = Signal(bool)
    editing_changed = Signal(bool)
    document_changing = Signal(object)
    edits_flush_requested = Signal()
    branch_scope_changed = Signal(str)
    page_changed = Signal(object)
    # 平移、缩放及视口尺寸变化后的 GUI 通知，宿主可合并后读取当前视图快照。
    viewport_changed = Signal()
    # 请求宿主展示所属文档的灵感侧栏；只改变停靠界面，不提交内容或切换文件。
    inspiration_sidebar_requested = Signal()
    # 拆出成功后仅提示侧栏，不切换画布；列表顺序由引擎根顺序决定。
    inspirations_detached = Signal(list)
    # 节点上的扩展图标只发出导航意图，具体页面由所属导图标签的 NodePageHost 打开。
    extension_page_requested = Signal(str)
    # 选线仅影响本标签。数量供宿主更新状态栏，不触发文档保存。
    graph_edge_selection_changed = Signal(int)

    def __init__(
        self,
        engine: MindMapEngine | None = None,
        parent=None,
        *,
        editable=True,
        toolbar=True,
        view_controls=False,
        embedded=False,
        ui_colors: UiColors | None = None,
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

        ui_colors 可预设宿主的最终界面色板，避免构造后立即再次换肤；省略时沿用
        文档主题。参数与 set_ui_colors 语义相同，不改变导图内容或撤销历史。
        """
        if ui_colors is not None and not isinstance(ui_colors, UiColors):
            raise TypeError("界面色板必须为 UiColors 或 None")
        super().__init__(parent)
        self.setObjectName("mindmap_canvas")
        self.setAutoFillBackground(True)
        self._ui_colors = None
        self._ui_colors_override = ui_colors
        self._embedded = embedded
        self.engine = engine or MindMapEngine()
        self.editable = editable
        self._properties_handler = None
        self._graph_layout_cache = None
        self._wheel_mode = "scroll"
        self._edraw_paste_options = ("append", True, True)
        self.factory = VisualFactory()
        self.layout_registry = (
            layout_registry if layout_registry is not None else create_default_layout_registry()
        )
        self.format_registry = (
            format_registry if format_registry is not None else create_default_format_registry()
        )
        self._disposed = False
        self._applying_engine_event = False
        self._init_selection()
        self._branch_drag_token = uuid4().hex
        # 宿主在同一文档绑定多个标签时启用；独立画布维持原有事件定位规则。
        self.shared_views = False
        # 保存通知也递增引擎修订号；拖放只冻结最近一次内容修订，自动保存不能使手势失效。
        self._branch_content_revision = self.engine.revision
        self._native_branch_drag = None
        self._committing_branch_drag = False
        self._view_reset_pending = False
        self._view_restore_center = None
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
        self._init_page_navigation(layout)
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
        self._detail_targets = {}
        self._detail_targets_dirty = True
        self._visual_document = None
        self._visuals = {}
        self._incremental_layout = None
        self._incremental_key = None
        self._fold_sides = {}
        self._virtualized = False
        self._viewport_signature = None
        self._selection_undo, self._selection_redo = [], []
        self._book_document = self._read_engine_document()
        self._document = self._book_document.page_view()
        self._page_chain = [None]
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
        self.select_nodes([self._document.root_id])
        self.reset_view()

    def dispose(self):
        """主动释放订阅与下载；多次调用安全，通常用于宿主移除标签页。"""
        if self._disposed:
            return
        self._cancel_branch_menu(restore=False)
        self.view.cancel_interaction()
        if self._native_branch_drag is not None:
            from PySide6.QtGui import QDrag

            QDrag.cancel()
        self._disposed = True
        self._detail_origin = None
        self._reference_origin = None
        self._detail_targets.clear()
        self._view_reset_pending = False
        self._view_restore_center = None
        self._view_reset_timer.stop()
        self.view._selection_update_timer.stop()
        self.view._selection_decoration.invalidate()
        self._unsubscribe()
        for reply in tuple(self._downloads):
            reply.abort()

    def _read_engine_document(self) -> MindMapDocument:
        """标准引擎捕获只读版本；扩展引擎继续经过原有 document 入口。

        子类可能在快照属性中附加投影或统计，不能因读取优化绕过宿主覆写；
        运行中替换标准类的属性也回退隔离快照，保持已有扩展的分发行为。
        """
        if type(self.engine) is MindMapEngine and MindMapEngine.document is _ENGINE_DOCUMENT:
            return self.engine.capture_read_snapshot()
        return self.engine.document

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
        if self.graph is not None:
            self.view.cancel_interaction()

    @document_interaction
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
            candidate = self.engine.page_document(self._current_page_id)
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
            bounds = QRectF()
            for key in ids:
                rect = self.layout_result.nodes.get(key)
                if rect is not None:
                    bounds = bounds.united(QRectF(rect.x, rect.y, rect.width, rect.height))
            self.view.ensureVisible(bounds, 35, 35)

    @property
    def interaction_id(self) -> str:
        """返回画布生命周期内稳定的交互来源标识；不属于文件内容或历史。

        面板可在直接调用引擎时使用 engine.event_source(interaction_id) 标记来源，
        异步回调须在实际提交时重新进入作用域。查询不分配资源或改变活动标签。
        """
        return self._branch_drag_token

    def is_external_event(self, event) -> bool:
        """判断是否为其他共享视图的内容提交；独立模式始终返回 False。

        shared_views 由宿主随关联视图数量更新。多视图下无来源的外部引擎更新也
        仅同步内容；saved、reset 与纯导航通知继续使用其原有生命周期语义。
        """
        return (self.shared_views and event.kind in {"change", "undo", "redo"}
                and event.source_id != self.interaction_id)

    @document_interaction
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

    def _create_actions(self):
        definitions = [
            ("add_inspiration", "新建想法", None, lambda w: w.add_inspiration(), True),
            ("move_to_inspiration", "转为独立想法", None, lambda w: w.move_selected_to_inspiration(), True),
            ("attach_inspirations", "加入导图…", None, lambda w: w.attach_selected_inspirations(), True),
            ("group_inspirations", "创建共同父主题", None, lambda w: w.group_selected_inspirations(), True),
            ("add_child", "添加子节点", "Tab", lambda w: w.add_child(), True),
            ("add_sibling", "添加同级节点", ["Return", "Enter"], lambda w: w.add_sibling(), True),
            ("insert_parent", "插入父节点", "Shift+Return", lambda w: w.insert_parent(), True),
            ("edit", "编辑文字", "F2", lambda w: w.edit_selected(), True),
            ("properties", "属性…", "Alt+Return", lambda w: w.edit_properties(), True),
            ("delete", "删除", "Delete", lambda w: w.delete_selected(), True),
            ("copy", "复制", "Ctrl+C", lambda w: w.copy(), False),
            ("cut", "剪切", "Ctrl+X", lambda w: w.cut(), True),
            ("paste", "粘贴", "Ctrl+V", lambda w: w.paste(), True),
            ("select_all", "全选", "Ctrl+A", lambda w: w.select_all_nodes(), False),
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
                lambda w: w.expand_all_trees(),
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
            ("create_submap", "创建子导图", None, lambda w: w.create_submap(), True),
            ("convert_descendants_to_submap", "后代转为子导图", None,
             lambda w: w.convert_descendants_to_submap(), True),
            ("convert_submap_to_descendants", "子导图转为节点后代", None,
             lambda w: w.convert_submap_to_descendants(), True),
            ("enter_submap", "进入子导图", None, lambda w: w.enter_submap(), False),
            ("favorite_submap", "收藏子导图", None, lambda w: w.toggle_submap_favorite(), True),
            ("delete_submap", "删除子导图…", None, lambda w: w.delete_submap(), True),
            ("create_detail_page", "添加详情页", None, lambda w: w.create_detail_page(), True),
            ("enter_detail_page", "查看详情页", None, lambda w: w.enter_detail_page(), False),
            ("delete_detail_page", "删除详情页…", None, lambda w: w.delete_detail_page(), True),
            ("set_page_reference", "引用页面…", None, lambda w: w.choose_page_reference(), True),
            ("open_page_reference", "打开引用页面", None, lambda w: w.open_page_reference(), False),
            ("clear_page_reference", "解除页面引用", None, lambda w: w.clear_page_reference(), True),
            ("zoom_in", "放大", "Ctrl+=", lambda w: w.view.zoom(1.2), False),
            ("zoom_out", "缩小", "Ctrl+-", lambda w: w.view.zoom(1 / 1.2), False),
            ("fit", "适应窗口", "Ctrl+0", lambda w: w.fit_to_view(), False),
            ("reset_zoom", "恢复默认尺寸", None, lambda w: w.reset_zoom(), False),
            ("center", "回到中心", "Home", lambda w: w.center_root(), False),
            ("wheel_mode", "滚轮行为", None, lambda w: None, False),
            ("wheel_scroll", "滚轮上下滚动", None, lambda w: w.set_wheel_mode("scroll"), False),
            ("wheel_zoom", "滚轮缩放", None, lambda w: w.set_wheel_mode("zoom"), False),
            ("branch_style", "分支连接线样式", None, lambda w: None, True),
            ("node_graph", "添加图", None, lambda w: w.open_node_graph(create=True), False),
            ("graph_text", "编辑图文本…", None, lambda w: w.edit_graph_text(), False),
            ("graph_add_edge", "添加连线…", None, lambda w: w.choose_graph_edge(), True),
            ("graph_auto_layout", "DOT 自动布局", None, lambda w: w.auto_layout_graph(), True),
            ("graph_remove", "删除此图…", None, lambda w: w.confirm_remove_graph(), True),
        ]
        for key, label, shortcut, callback, mutating in definitions:
            self.register_action(key, label, callback, shortcut=shortcut, mutating=mutating)
        self._actions["reset_zoom"].setToolTip("恢复默认尺寸（100%）")
        graph_layout_action = self._actions["graph_auto_layout"]
        graph_layout_action.setToolTip("清除手动坐标，重排整张图并适应窗口")
        graph_layout_action.setVisible(False)
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
        # 全选只改变当前页的节点选择，只读画布仍可用；编辑期间交回正文控件的
        # 原生 Ctrl+A，空页面则没有可选择的节点。
        self._actions["select_all"].setEnabled(not editing and bool(self._document.nodes))
        self._update_graph_actions(editing)
        if self.graph is not None:
            self._sync_navigation()
            return
        if self._properties_handler is not None:
            self._actions["properties"].setEnabled(True)
        self._actions["branch_style"].setEnabled(
            self.editable
            and not editing
            and self.layout_registry.info(self._document.layout.algorithm_id).supports_branch_styles
        )
        self._sync_branch_actions()
        node = self._document.nodes.get(self.current_node_id())
        image_only = bool(node and node.is_image_node)
        referenced = bool(node and node.page_reference_root_id)
        self._actions["edit"].setEnabled(self.editable and not editing and not image_only and not referenced)
        self._actions["remove_image"].setVisible(not image_only)
        for name in ("image", "image_url"):
            self._actions[name].setEnabled(bool(self.editable and not editing and node and not node.submap_id))
        single = self.selection_count <= 1
        in_detail = self.current_page_kind == "detail"
        in_inspiration = self.current_page_kind == "inspiration"
        selected = self.selection_count
        eligible = bool(selected == 1 and node and not image_only and node.id not in self._document.root_ids)
        reference = self._actions["set_page_reference"]
        reference.setText("更换引用页面…" if referenced else "引用页面…")
        reference.setVisible(eligible)
        reference.setEnabled(eligible and not editing and self.editable)
        for name in ("open_page_reference", "clear_page_reference"):
            action = self._actions[name]
            action.setVisible(eligible and referenced)
            action.setEnabled(eligible and referenced and not editing
                              and (name == "open_page_reference" or self.editable))
        for name in ("attach_inspirations", "group_inspirations"):
            self._actions[name].setVisible(in_inspiration)
            self._actions[name].setEnabled(bool(self.editable and not editing and selected))
        self._actions["move_to_inspiration"].setVisible(not in_detail)
        self._actions["move_to_inspiration"].setEnabled(bool(
            self.editable and not editing and selected and not in_detail
            and (in_inspiration or not self.is_node_selected(self._document.root_id))))
        if not node:
            for name in ("add_child", "add_sibling", "insert_parent", "edit", "delete", "copy", "cut",
                         "fold", "expand_all", "collapse_all", "move_up", "move_down", "hyperlink"):
                self._actions[name].setEnabled(False)
        converting = self._actions["convert_descendants_to_submap"]
        converting.setVisible(not in_detail and not image_only)
        converting.setEnabled(bool(self.editable and not editing and selected == 1 and node
                                   and node.children and not node.submap_id and not in_detail and not image_only))
        converting.setToolTip("保留当前节点，将全部普通后代迁入同名子导图并进入")
        restoring = self._actions["convert_submap_to_descendants"]
        restoring.setVisible(not in_detail and not image_only)
        restoring.setEnabled(bool(self.editable and not editing and selected == 1 and node
                                  and node.submap_id and not in_detail and not image_only))
        restoring.setToolTip("仅转换当前一层；根有独立内容时保留为普通子节点")
        # 结束已失效的共享编辑器时，旧选择可能已经从引擎删除而读模型尚未合并。
        # 动作借用当前读模型对应的解析缓存，正式刷新后再同步新的共享详情目标。
        detail_page = (self._detail_targets.get(node.id, (node.id, node.detail_page_id))[1]
                       if node and not in_detail else None)
        self._update_node_page_actions(node if single else None, editing, creation_allowed=not in_detail,
                                       show_creation=not image_only, detail_page=detail_page if single else None)
        self._sync_navigation()

    def _update_node_page_actions(self, node, editing, *, creation_allowed, show_creation, detail_page):
        """按当前节点、页面权限和正文编辑状态同步两类树页动作。

        图页传入局部投影，树页传入当前可操作节点和解析后的共享详情。进入已有页只
        需要有效目标；创建、删除和收藏要求可编辑。动作由画布拥有，供菜单共用。
        """
        actions = self._actions
        can_edit = self.editable and not editing
        submap = node.submap_id if node is not None else None
        show = creation_allowed and show_creation
        create_submap = actions["create_submap"]
        create_submap.setVisible(show and (self.graph is None or not submap))
        create_submap.setEnabled(bool(show and can_edit and node and not node.children and not submap))
        create_submap.setToolTip("仅没有普通子节点和子导图的节点可创建")
        create_detail = actions["create_detail_page"]
        create_detail.setVisible(show and not detail_page)
        create_detail.setEnabled(bool(show and can_edit and node and not detail_page))
        for kind, page_id in (("detail_page", detail_page), ("submap", submap)):
            for prefix in ("enter", "delete"):
                action = actions[f"{prefix}_{kind}"]
                action.setVisible(bool(page_id) or (kind == "submap" and self.graph is None))
                action.setEnabled(bool(page_id and not editing and (prefix == "enter" or self.editable)))
        favorite = actions["favorite_submap"]
        favorite.setVisible(bool(submap))
        favorite.setEnabled(bool(submap and can_edit))
        checked = bool(submap and self.engine.is_submap_favorite(submap))
        favorite.setText("取消收藏子导图" if checked else "收藏子导图")
        set_action_icon(favorite, "favorite_on" if checked else "favorite", self._ui_colors)

    def set_editable(self, editable: bool) -> None:
        """切换只读模式，取消尚未提交的文本编辑与下载后续写入。"""
        changed = self.editable != bool(editable)
        if not editable:
            self._cancel_branch_menu()
            self.finish_edit(commit=False)
        self.editable = editable
        self._update_actions()
        self.view.cancel_interaction()
        if not editable and self._native_branch_drag is not None:
            from PySide6.QtGui import QDrag

            QDrag.cancel()
        if changed:
            self.editable_changed.emit(bool(editable))

    def _ensure_editable(self):
        if not self.editable:
            raise MindMapError("当前画布为只读模式")

    def _engine_changed(self, event):
        """引擎已提交而读模型尚未同步时，暂停路径查询，避免访问刚删除的旧选择。"""
        if self._disposed:
            return
        self._applying_engine_event = True
        try:
            if event.changes is not None and any(
                    event.changes.collections.get(name) for name in ("submaps", "detail_pages")):
                self._detail_targets_dirty = True
            if event.kind != "saved":
                self._branch_content_revision = event.revision
            if (event.kind != "saved" and self._native_branch_drag is not None
                    and not self._committing_branch_drag):
                from PySide6.QtGui import QDrag

                QDrag.cancel()
            related = event.changes is not None and (
                self.current_page_kind == "inspiration" or "inspiration" in event.changes.settings)
            related = related and not self.is_external_event(event)
            before = self._history_view_snapshot() if event.kind == "change" and related else None
            self._apply_engine_event(event)
            if event.kind == "change":
                self._view_undo.append((before, self._history_view_snapshot() if related else None))
                del self._view_undo[:-self.engine.history_limit]
                self._view_redo.clear()
            elif event.kind in ("undo", "redo"):
                source = self._view_undo if event.kind == "undo" else self._view_redo
                target = self._view_redo if event.kind == "undo" else self._view_undo
                if source:
                    pair = source.pop()
                    target.append(pair)
                    snapshot = pair[0 if event.kind == "undo" else 1]
                    if snapshot is not None and not self.is_external_event(event):
                        self._restore_history_view(snapshot)
            elif event.kind == "reset":
                self._view_undo.clear()
                self._view_redo.clear()
        finally:
            self._applying_engine_event = False
        self._sync_navigation()

    @property
    def ui_colors(self) -> UiColors:
        """返回当前界面色板，供同一导图的节点详情页复用，不暴露可写主题。"""
        return ui_colors(self._ui_colors_override or self._document.theme)

    def set_ui_colors(self, colors: UiColors | None) -> None:
        """覆盖本组件的界面色板，不修改导图主题、场景内容、选择和撤销历史。

        None 恢复原有跟随导图主题的行为。只刷新控件外观，保留原地编辑器、
        属性弹层、鼠标位置和画布倍率；错误类型抛出 TypeError。
        """
        if colors is not None and not isinstance(colors, UiColors):
            raise TypeError("界面色板必须为 UiColors 或 None")
        previous_background = self.scene.backgroundBrush().color()
        self._ui_colors_override = colors
        self._apply_ui_theme()
        if self.graph is not None:
            if previous_background != self.scene.backgroundBrush().color():
                self._set_branch_items(self.layout_result)
            self.view.viewport().update()

    def _canvas_background(self, colors: UiColors) -> str:
        """解析屏幕画布的实际底色，供场景背景与图页连线配色同步使用。

        嵌入画布跟随宿主的编辑区域，独立画布跟随当前页面的文档主题。返回值仅供
        界面渲染，导出仍从文档读取背景。
        """
        return colors.field if self._embedded else self._document.theme.background

    def _apply_ui_theme(self):
        theme = self._ui_colors_override or self._document.theme
        colors = ui_colors(theme)
        # 嵌入模式只覆盖屏幕底色；导出重新从文档构建场景，仍使用文档背景。
        # 每次文档刷新也同步此处，避免打开文件或撤销后闪回旧的内部卡片背景。
        background = self._canvas_background(colors)
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
        set_action_icon(self._actions["reset_zoom"], "reset_zoom", theme)
        self._sync_wheel_actions()
        self._sync_branch_actions()
        self.path_navigation.set_ui_colors(colors)

    def _branch_selection(self) -> list[str]:
        return self.selected_node_ids() or ([self.focus_id] if self.focus_id else list(self._document.root_ids))

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
        if self.graph is not None:
            return set()
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
        if not self.layout_registry.info(self._document.layout.algorithm_id).supports_branch_styles:
            raise MindMapError("当前布局算法不支持分支连接线样式")

    def preview_branch_style(self, kind: BranchStyle | str | None, *, scope="subtree") -> None:
        """在独立候选文档上预览，只借用节点尺寸，不修改正式文档和历史。

        已关闭/禁用组件可能仍收到离开或聚焦事件；这些迟到事件直接丢弃。
        导出始终读取引擎快照，因此预览不会泄漏到 PNG、SVG、PDF 或保存文件。
        """
        if self._disposed or not self._actions["branch_style"].isEnabled():
            return
        candidate = self.engine.page_document(self._current_page_id)
        apply_branch_style(candidate, self._branch_selection(), kind, scope=scope)
        result = self.layout_registry.layout(candidate, self._measurements, focus_id=self.focus_id)
        self._preview_layout = result
        self._show_branch_layout(result)

    @document_interaction
    def set_branch_style(self, kind: BranchStyle | str | None, *, scope: str = "subtree") -> None:
        """先试算再一次提交发出的连接线设置，失败不改变文档、历史与选择。

        kind 接受枚举、语义名称或 None（恢复旧原始曲线）。默认 subtree 显式更新
        全部后代，level 仅改所选主题；不使用跨层隐式继承。优先采用当前多选，
        没有选择时采用聚焦主题，否则采用根主题；文字编辑、只读与不支持的布局拒绝操作。
        """
        self._ensure_branch_editable()
        ids = self._branch_selection()
        candidate = self.engine.page_document(self._current_page_id)
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
        self._view_restore_center = None
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
        """在有效布局上消费一次初始定位或恢复请求；隐藏及释放后不触碰视图。"""
        if (self._disposed or not self._view_reset_pending or not self.view.isVisible()
                or self.view.viewport().size().isEmpty() or self._refreshing):
            return
        center = self._view_restore_center
        reset_zoom = center is None
        if center is None:
            if self.graph is not None:
                center = QPointF(*self.layout_result.bounds.center)
            else:
                root = self.node_items.get(self.focus_id or self._document.root_id)
                if root is None or root.sceneBoundingRect().isEmpty():
                    return
                center = root.sceneBoundingRect().center()
        # 先消费请求再调整视图，避免变换引发的尺寸事件重复排程。普通初始化
        # 使用 100% 根中心，退出快照则保留已经设置的倍率并使用目标场景中心。
        self._view_reset_pending = False
        self._view_restore_center = None
        self.view._hidden_center = None
        if reset_zoom:
            self.view.resetTransform()
        self.view.update_navigation_rect(center=center)
        self.view.update_cursor()

    @property
    def zoom_factor(self) -> float:
        """读取当前视图实际倍率，1.0 表示 100%。在 GUI 线程配合 viewport_changed 使用。"""
        return self.view.transform().m11()

    def reset_zoom(self) -> None:
        """立即恢复到 100% 原始尺寸，围绕当前视口中心缩放，不主动居中根节点。

        复用交互缩放入口，按当前倍率计算恢复因子，并在恢复后重算一屏导航余量；
        当前查看位置超出新倍率下的可平移范围时，沿用视图已有的边界约束。
        本接口只调整视图，不提交输入，不修改文档、选择、折叠状态或撤销历史。
        已释放的画布忽略请求。对应动作在正文编辑期间禁用，只读状态下仍可使用。
        """
        if not self._disposed:
            center = self.view.mapToScene(self.view.viewport().rect()).boundingRect().center()
            self.view.zoom(1 / self.view.transform().m11())
            # 交互入口使用整数视口锚点，低倍率下的半像素取整会放大为场景偏移；
            # 恢复时以完整可见区域的浮点中心校正位置，再由导航范围钳制越界中心。
            self.view.update_navigation_rect(center=center)

    def center_root(self):
        """将当前页根节点或导航临时显示的中心节点置于视口中央，保持用户的缩放比例。"""
        if self.graph is not None and self._view_reset_pending and self.view.isVisible():
            self._apply_view_reset()
        key = self.focus_id or self._document.root_id
        rect = (self.layout_result.bounds if self.current_page_kind in ("inspiration", "graph")
                else self.layout_result.nodes.get(key))
        if not self._disposed and rect is not None:
            self.view.update_navigation_rect(center=QRectF(rect.x, rect.y, rect.width, rect.height).center())

    def fit_to_view(self):
        """按可见内容适应窗口，将最终倍率限制在 10%～200%，不计入导航余量。

        先提交正在编辑的节点，再按更新后的布局计算。达到最小倍率时保留内容
        中心居中，允许超大导图的边缘超出视口，由用户平移查看，不继续缩小。
        按钮和快捷键均复用此入口，与交互缩放共用同一最小倍率。
        """
        self.finish_edit(commit=True)
        if self.graph is not None and self._view_reset_pending and self.view.isVisible():
            self._apply_view_reset()
        if self.graph is not None and not self.graph.nodes:
            return
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

    @document_interaction
    def add_child(self):
        """为当前节点添加子节点并开始编辑正文，返回新节点标识。

        只读时拒绝操作；节点属性及分支继承遵循引擎规则，正文通过独立编辑会话提交。
        """
        if self.graph is not None:
            return self.add_graph_node(connect_from_selection=True)
        self._ensure_editable()
        key = self.engine.add_child(self.current_node_id())
        self.begin_edit(key)
        return key

    @document_interaction
    def add_sibling(self):
        """在当前节点旁添加同级主题并开始正文编辑，返回新节点标识。

        同级顺序、方向继承及根节点限制统一由引擎校验，失败不保留部分结构修改。
        """
        if self.graph is not None:
            return self.add_graph_node()
        self._ensure_editable()
        key = self.engine.add_sibling(self.current_node_id())
        self.begin_edit(key)
        return key

    @document_interaction
    def insert_parent(self):
        """为当前节点插入父主题并开始编辑，返回新增主题标识。

        只读、非法根操作或引擎钩子拒绝时抛出异常，不绕过引擎的事务及撤销边界。
        """
        self._ensure_editable()
        key = self.engine.insert_parent(self.current_node_id())
        self.begin_edit(key)
        return key

    @document_interaction
    def delete_selected(self):
        """一次删除明确选中的连线或节点，复用引擎的撤销和跨视图同步。

        图页只删除当前实际选中的对象。选线时保留节点和未选中的线；选节点时
        一同删除其相连的线。普通导图的节点子树及关联资源共用一条撤销记录。
        """
        self._ensure_editable()
        ids = self.selected_node_ids()
        if self.graph is not None:
            edges = self.view.graph_edge_edit.selected_edges
            if edges:
                self._delete_graph_edges(edges)
            elif ids:
                self._delete_graph_nodes(ids)
            return
        if ids:
            nodes = {key for root in ids for key in self._book_document.owned_descendants(root)}
            submaps = sum(bool(self._book_document.nodes[key].submap_id) for key in nodes)
            details = sum(bool(self._book_document.nodes[key].detail_page_id) for key in nodes)
            graphs = sum(GRAPH_PAGE_TYPE in self._book_document.nodes[key].extension_pages for key in nodes)
            if submaps or details or graphs:
                generation, revision = self.engine.document_generation, self._branch_content_revision
                choice = QMessageBox.question(
                    self, "删除节点及附属页面",
                    f"将删除 {len(nodes)} 个节点，以及其中的 {submaps} 张子导图、{details} 张详情页、{graphs} 张图。\n"
                    "删除后可以撤销。",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
                if choice != QMessageBox.StandardButton.Yes:
                    return
                self._ensure_editable()
                if self._disposed or (generation, revision) != (
                        self.engine.document_generation, self._branch_content_revision):
                    raise MindMapError("确认期间导图发生变化，请重新删除")
            self.engine.remove_nodes(ids)

    @document_interaction
    def toggle_expanded(self):
        """切换当前节点的文档折叠状态，通过引擎提交，支持撤销、重做及自动保存。"""
        self._ensure_editable()
        node = self.engine.get_node(self.current_node_id())
        self.engine.set_expanded(node.id, not node.expanded)

    def _check_image_parent(self, node_id: str) -> None:
        """同步验证图片父节点；只读、已释放、缺失节点及子导图入口抛出 MindMapError。

        不读取或提交输入，必须在有效 GUI 线程内调用。node_id 可属于非活动页面；
        查询借用引擎快照，不缓存节点引用，不改变文档、选择或历史。
        """
        self._ensure_editable()
        if self._disposed:
            raise MindMapError("画布已释放，不能添加图片")
        if self.engine.get_node(node_id).submap_id:
            raise MindMapError("此节点已有子导图，请进入子导图添加图片")

    @document_interaction
    def _insert_image_child(self, node_id: str, data: bytes, media_type: str) -> str:
        """先验证图片及输入，再原子创建纯图片子节点，返回新节点 ID。

        node_id 是父节点；data 为独立图片字节，media_type 为读取适配层识别的 MIME，
        这里再次解码取得实际像素尺寸及 MIME，保留原始字节与透明通道。有效草稿独立
        提交，创建只产生一次历史；活动页中的新节点会被选中，非活动页不抢占选择。
        校验、只读、输入法候选或同步提交导致目标失效时抛出 MindMapError。仅在 GUI
        线程、画布存活时同步调用，不持有系统 MIME 对象，也不创建后台任务。
        """
        self._check_image_parent(node_id)
        image, media_type = decode_image(data)
        generation, page_id = self.engine.document_generation, self.current_page_id
        parent_page = self.engine.node_page_id(node_id)
        selection = self.selected_node_ids()

        def check():
            self._check_image_parent(node_id)
            if (generation != self.engine.document_generation or page_id != self.current_page_id
                    or parent_page != self.engine.node_page_id(node_id)):
                raise MindMapError("添加图片期间导图或目标已发生变化，请重新操作")
            if self.is_composing or not all(guard() for guard in tuple(self._navigation_guards)):
                raise MindMapError("请先完成输入法候选，再添加图片")

        check()
        self.edits_flush_requested.emit()
        self.finish_edit(commit=True)
        check()
        key = self.engine.add_image_child(node_id, data, media_type, width=image.width())
        if parent_page == self.current_page_id:
            self.select_nodes([key])
        else:
            self.select_nodes(selection)
        if self._selection_undo:
            self._selection_undo[-1] = (
                selection.copy() if parent_page == page_id else [node_id], [key])
        self._update_history_view_after()
        self.image_imported.emit(key)
        return key

    def edit_properties(self):
        """打开当前节点属性入口；宿主接管时转交侧栏，否则使用共享属性弹窗。

        弹窗确认后先验证完整候选，再以正式事务提交；取消不修改文档和历史。
        """
        if self.graph is not None:
            return
        if self._properties_handler is not None:
            self._properties_handler()
            return
        self._ensure_editable()
        key = self.current_node_id()
        result = node_properties(
            self,
            self._document.nodes[key],
            branch_styles=self.layout_registry.info(
                self._document.layout.algorithm_id
            ).supports_branch_styles,
            branch_scope=self._branch_scope,
            theme=self._document.theme,
            root_id=self._document.root_id,
            parents=self._document.parents(),
        )
        if result is not None:
            candidate = self.engine.page_document(self._current_page_id)
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

    def import_image(self, node_id: str, path: str | Path) -> None:
        """读取本地图片并在 node_id 父节点下创建纯图片节点，返回 None。

        path 为本机图片路径；创建前同步校验图片和父节点，默认使用原始像素尺寸。
        文件读取错误抛出 OSError，图片、父节点、只读或输入法错误抛出 MindMapError，
        校验失败不提交草稿。成功发出携带新节点 ID 的 image_imported；仅在 GUI
        线程、组件释放前调用，不保留文件句柄，历史及资源由引擎管理。
        """
        self._check_image_parent(node_id)
        data = Path(path).read_bytes()
        self._insert_image_child(node_id, data, "")

    def import_image_url(self, node_id: str, url: str) -> QNetworkReply:
        """从 HTTP(S) 网址下载图片并在 node_id 下创建子节点，返回 QNetworkReply。

        node_id 是原始父节点，url 必须为有效 HTTP(S) 地址；启动前无效目标、只读、
        已释放或非法网址抛出 MindMapError。完成时重新校验原父节点及文档代次，
        图片或输入错误通过 error_occurred 报告，不留下半成品。切页不抢占活动页，
        成功的 image_imported 携带新节点 ID。仅在 GUI 线程使用；reply 由本组件
        网络管理器拥有，完成后 deleteLater，调用方不得缓存到完成之后。dispose
        取消未完成下载，文档替换、只读和父节点删除会使过期结果失效。
        """
        self._check_image_parent(node_id)
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
                self._insert_image_child(node_id, data, "")
            except (MindMapError, OSError) as exc:
                self.error_occurred.emit(str(exc))
            finally:
                reply.deleteLater()

        reply.finished.connect(finished)
        return reply

    def choose_image(self) -> None:
        """打开文件选择器，在当前父节点下导入纯图片子节点，返回 None。

        无参数；取消保持原状，成功复用 import_image() 的校验、历史及异常规则。
        仅在 GUI 线程、画布存活时调用；对话框在同步调用结束时释放，不保留文件句柄。
        """
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", "图片 (*.png *.jpg *.jpeg *.webp *.gif *.svg)"
        )
        if path:
            self.import_image(self.current_node_id(), path)

    def choose_image_url(self) -> None:
        """询问网址，在当前父节点下异步创建纯图片子节点，返回 None。

        无参数；取消不创建下载或修改文档，复用 import_image_url() 的校验及异常
        规则。仅在 GUI 线程、画布存活时调用；对话框同步释放，下载由本画布管理。
        """
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
        document = self.engine.page_document(self._current_page_id,
                                             inspiration_root_id=self.current_inspiration_root_id)
        if self.graph is not None:
            def layout_provider(sizes):
                automatic = build_graph_layout(sizes, document.graph.edges, color=document.theme.accent)
                return graph_layout(document.graph, automatic)

            export_document(document, path, layout_registry=self.layout_registry, scale=scale,
                            layout_provider=layout_provider)
            return
        export_document(
            document,
            path,
            layout_registry=self.layout_registry,
            expand_all=expand_all,
            scale=scale,
            detail_page_resolver=self.engine.resolve_detail_page,
        )

    @document_interaction
    def set_layout(self, config: LayoutConfig) -> None:
        """在提交前试算新布局，缺失算法、非法参数或计算失败均不产生部分修改。"""
        self._ensure_editable()
        candidate = self.engine.page_document(self._current_page_id)
        candidate.layout = config
        self.validate_document(candidate)
        self.engine.set_layout(config, page_id=self._current_page_id)

    def validate_document(self, document: MindMapDocument, *, defer_inactive_pages: bool = False,
                          validation: ValidatedDocument | None = None) -> None:
        """替换前验证算法、图片及包含共享图标的实际尺寸，失败时保留正式状态。

        完整文件使用候选快照的只读解析器解析页面关系，不借用旧文档的同名标识。
        单页候选缺少页外归属表，已有节点借用当前解析，新粘贴节点使用候选自身
        引用；普通内容、外观及布局仍完全取候选。查询不提交引擎历史或导航。

        文件打开可传 defer_inactive_pages=True：全文件结构、所有页面配置及使用中的
        图片仍立即验证；内置树布局的非活动页面延迟到首次进入时排版。自定义算法、
        注册表覆写及单页编辑候选始终保留完整试算，避免绕过宿主的校验及副作用隔离。

        validation 仅供标准原生打开链路复用完整结构校验，须持有同一独占且未修改的
        文档。预检只读借用凭据，不消费或发布可写引用；图片解码、页面配置和几何
        数值检查照常执行。扩展布局或修改候选的调用方须省略凭据以重新完整校验。
        """
        if validation is None:
            document.validate()
        else:
            if type(validation) is not ValidatedDocument:
                raise TypeError("加载校验凭据必须为 ValidatedDocument")
            validation.require_document(document)
        if isinstance(document, MindMapPageView):
            def resolver(key):
                return (self.engine.resolve_detail_page(key) if key in self._book_document.nodes
                        else (key, document.nodes[key].detail_page_id))
        else:
            resolver = document.detail_page_resolver() if document.submaps else None
        if defer_inactive_pages and not isinstance(document, MindMapPageView):
            # 配置及图片不依赖正文排版，先逐项校验，损坏的非活动页也必须在替换前拒绝。
            records = [document, *document.iter_pages(), document.inspiration]
            for record in records:
                self.layout_registry.validate_config(record.layout)
            image_ids = {node.image_id for node in document.nodes.values() if node.image_id}
            for key in image_ids:
                if key not in self.factory.pixmaps:
                    self.factory.pixmaps[key] = decode_image(document.assets[key].data)[0]
                    if len(self.factory.pixmaps) > 128:
                        self.factory.pixmaps.popitem(last=False)
            pages = [document.page_view()]
            pages.extend(document.page_view(page.id) for page in document.iter_pages()
                         if not supports_incremental_layout(self.layout_registry, page.layout.algorithm_id))
        else:
            pages = [document.page_view()] + [document.page_view(page.id) for page in document.iter_pages()]
        # 单页候选只测量当前页；文件打开只预排主图及扩展页面，禁止混排所有页面节点。
        for page in pages:
            self.layout_registry.validate_config(page.layout)
            _, sizes = self.factory.measure(page, detail_page_resolver=resolver)
            if (defer_inactive_pages
                    and supports_incremental_layout(self.layout_registry, page.layout.algorithm_id)):
                # 与正式画布共用几何服务，避免为首帧不会绘制的连接线构造全部路径。
                prepared = create_incremental_layout(self.layout_registry, page, sizes)
                # 冻结结果会检查摘要边界，数值溢出也必须在替换前拒绝；此处不会
                # 展开节点坐标或连接线路径，成本与视口外路径数量无关。
                if prepared is not None:
                    prepared.result()
                else:
                    self.layout_registry.layout(page, sizes)
            else:
                self.layout_registry.layout(page, sizes)

    def context_menu(self, global_position, *, on_canvas: bool = False):
        """在屏幕全局位置显示菜单。鼠标命中背景时传 on_canvas=True，仅显示页面级动作。

        默认按当前选择区分单节点及多选，键盘可直接调用。复用原动作及权限。关闭后
        释放临时菜单和子菜单，动作仍由画布持有。调用需在存活画布的 GUI 线程执行。
        """
        menu = QMenu(self)
        try:
            for title, keys in self._context_menu_groups(on_canvas or not self.selection_count):
                actions = [self._actions[key] for key in keys if self._actions[key].isVisible()]
                if not actions:
                    continue
                if title:
                    menu.addMenu(title).addActions(actions)
                else:
                    if menu.actions():
                        menu.addSeparator()
                    menu.addActions(actions)
            menu.exec(global_position)
        finally:
            if isValid(menu):
                menu.deleteLater()

    def _context_menu_groups(self, on_canvas):
        if self.graph is not None:
            return self._graph_context_menu_groups(on_canvas)
        if on_canvas:
            groups = [
                ("", ("fit", "center", "reset_zoom", "zoom_in", "zoom_out")),
                ("", ("expand_all", "collapse_all")),
            ]
            if self.current_page_kind == "inspiration":
                groups.insert(0, ("", ("add_inspiration",)))
            return groups
        if self.selection_count > 1:
            return [
                ("", ("cut", "copy", "delete")),
                ("灵感", ("move_to_inspiration", "attach_inspirations", "group_inspirations")),
                ("", ("undo", "redo")),
                ("", self._custom_actions),
            ]
        return [
            ("", ("add_child", "add_sibling", "edit", "properties")),
            ("", ("cut", "copy", "paste", "delete")),
            ("节点结构", ("insert_parent", "move_up", "move_down", "fold")),
            ("页面", ("create_submap", "convert_descendants_to_submap", "enter_submap",
                      "convert_submap_to_descendants", "favorite_submap", "delete_submap",
                      "create_detail_page", "enter_detail_page", "delete_detail_page",
                      "set_page_reference", "open_page_reference", "clear_page_reference")),
            ("", ("node_graph",)),
            ("灵感", ("add_inspiration", "move_to_inspiration", "attach_inspirations", "group_inspirations")),
            ("图片和链接", ("image", "image_url", "remove_image", "hyperlink")),
            ("", ("undo", "redo")),
            ("", self._custom_actions),
        ]
