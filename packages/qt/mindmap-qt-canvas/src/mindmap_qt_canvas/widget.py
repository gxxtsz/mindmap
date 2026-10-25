"""可嵌入宿主窗口的完整思维导图组件。"""

from __future__ import annotations

from collections import ChainMap
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

from mindmap_core import (
    BranchStyle,
    ChangeEvent,
    ChangeSet,
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
from mindmap_formats import (
    create_default_format_registry,
    decode_document,
    detect_outline_format,
    encode_document,
)
from mindmap_layouts import (
    create_default_layout_registry,
    create_incremental_layout,
    supports_incremental_layout,
)
from mindmap_qt_render.exporting import export_document
from mindmap_qt_render.rendering import BranchItem, VisualFactory, decode_image
from mindmap_qt_ui.branch_styles import BranchStyleMenu, branch_style_icon
from mindmap_qt_ui.dialogs import EdrawPasteOptions, edraw_paste_options, node_properties
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
    QMessageBox,
    QToolBar,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from .clipboard import (
    EDRAW_CLIPBOARD_MIME,
    has_clipboard_image,
    read_clipboard_image,
    read_edraw_clipboard,
)
from .inspiration import InspirationCanvasMixin
from .interactions import document_interaction
from .items import NodeEditor, NodeItem
from .navigation import PageNavigationMixin
from .view import MIN_ZOOM, MindMapView
from .view_controls import _ViewControls

CLIPBOARD_MIME = "application/x-python-mindmap+json"
VIRTUALIZATION_NODE_LIMIT = 1000


class MindMapWidget(InspirationCanvasMixin, PageNavigationMixin, QWidget):
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
    # 请求宿主展示所属文档的灵感侧栏；只改变停靠界面，不提交内容或切换文件。
    inspiration_sidebar_requested = Signal()
    # 拆出成功后仅提示侧栏，不切换画布；列表顺序由引擎根顺序决定。
    inspirations_detached = Signal(list)

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
        self._visual_document = None
        self._visuals = {}
        self._incremental_layout = None
        self._incremental_key = None
        self._fold_sides = {}
        self._virtualized = False
        self._viewport_signature = None
        self._selection_undo, self._selection_redo = [], []
        self._book_document = self.engine.document
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
        self._view_reset_pending = False
        self._view_restore_center = None
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
    def is_composing(self) -> bool:
        """只读查询原地编辑器是否还有输入法候选；没有编辑器时返回 False。

        宿主在保存、另存或关闭前查询，不需要持有内部 NodeEditor。查询不提交
        候选、不结束草稿、不改变焦点；候选完成后再调用正常的输入提交接口。
        """
        return self.editor is not None and self.editor.composing

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
            bounds = self.node_items[ids[0]].sceneBoundingRect()
            for key in ids[1:]:
                bounds = bounds.united(self.node_items[key].sceneBoundingRect())
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

    @property
    def edraw_paste_options(self) -> tuple[str, bool, bool]:
        """返回亿图、缩进文本与 Markdown 共用的粘贴选项，初始为 (append, True, True)。

        三项分别为粘贴方式、保留原后代及替换根节点；接口名沿用亿图版本保持兼容。
        此值只属于当前组件，不进入导图文件或撤销历史。宿主可监听
        edraw_paste_options_changed(str, bool, bool) 保存，再通过 setter 恢复跨重启偏好。
        根节点指当前选中的目标主题；三种来源每次均询问，多根追加不会覆盖单树偏好。
        """
        return self._edraw_paste_options

    def set_edraw_paste_options(self, mode: str, keep_children: bool, replace_root: bool = True) -> None:
        """设置三种外部大纲共用的默认选项；非法值抛出 MindMapError，不修改文档。

        mode 仅允许 append、replace，keep_children 和 replace_root 必须是布尔值。
        replace_root 默认 True，已有二参数调用仍沿用替换目标正文的行为。实际改变时
        才发送三参数信号，适合宿主 QSettings 持久化；恢复偏好不受只读或编辑状态限制。
        沿用亿图接口名和信号，宿主继续使用既有配置项同步所有画布，无需迁移。
        """
        if (mode not in ("append", "replace") or not isinstance(keep_children, bool)
                or not isinstance(replace_root, bool)):
            raise MindMapError("粘贴方式必须为 append 或 replace，保留后代及替换根节点选项必须为布尔值")
        options = (mode, keep_children, replace_root)
        if options != self._edraw_paste_options:
            self._edraw_paste_options = options
            self.edraw_paste_options_changed.emit(*options)

    def _create_actions(self):
        definitions = [
            ("add_inspiration", "新建想法", None, lambda w: w.add_inspiration(), True),
            ("move_to_inspiration", "转为独立想法", None, lambda w: w.move_selected_to_inspiration(), True),
            ("attach_inspirations", "加入导图…", None, lambda w: w.attach_selected_inspirations(), True),
            ("group_inspirations", "创建共同父主题", None, lambda w: w.group_selected_inspirations(), True),
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
            ("enter_submap", "进入子导图", None, lambda w: w.enter_submap(), False),
            ("favorite_submap", "收藏子导图", None, lambda w: w.toggle_submap_favorite(), True),
            ("delete_submap", "删除子导图…", None, lambda w: w.delete_submap(), True),
            ("create_detail_page", "添加详情页", None, lambda w: w.create_detail_page(), True),
            ("enter_detail_page", "查看详情页", None, lambda w: w.enter_detail_page(), False),
            ("delete_detail_page", "删除详情页…", None, lambda w: w.delete_detail_page(), True),
            ("zoom_in", "放大", "Ctrl+=", lambda w: w.view.zoom(1.2), False),
            ("zoom_out", "缩小", "Ctrl+-", lambda w: w.view.zoom(1 / 1.2), False),
            ("fit", "适应窗口", "Ctrl+0", lambda w: w.fit_to_view(), False),
            ("reset_zoom", "恢复默认尺寸", None, lambda w: w.reset_zoom(), False),
            ("center", "回到中心", "Home", lambda w: w.center_root(), False),
            ("wheel_mode", "滚轮行为", None, lambda w: None, False),
            ("wheel_scroll", "滚轮上下滚动", None, lambda w: w.set_wheel_mode("scroll"), False),
            ("wheel_zoom", "滚轮缩放", None, lambda w: w.set_wheel_mode("zoom"), False),
            ("branch_style", "分支连接线样式", None, lambda w: None, True),
        ]
        for key, label, shortcut, callback, mutating in definitions:
            self.register_action(key, label, callback, shortcut=shortcut, mutating=mutating)
        self._actions["reset_zoom"].setToolTip("恢复默认尺寸（100%）")
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
        node = self._document.nodes.get(self.current_node_id())
        image_only = bool(node and node.is_image_node)
        self._actions["edit"].setEnabled(self.editable and not editing and not image_only)
        self._actions["remove_image"].setVisible(not image_only)
        for name in ("image", "image_url"):
            self._actions[name].setEnabled(bool(self.editable and not editing and node and not node.submap_id))
        single = len(self.selected_node_ids()) <= 1
        in_detail = self.current_page_kind == "detail"
        in_inspiration = self.current_page_kind == "inspiration"
        selected = self.selected_node_ids()
        for name in ("attach_inspirations", "group_inspirations"):
            self._actions[name].setVisible(in_inspiration)
            self._actions[name].setEnabled(bool(self.editable and not editing and selected))
        self._actions["move_to_inspiration"].setVisible(not in_detail)
        self._actions["move_to_inspiration"].setEnabled(bool(
            self.editable and not editing and selected and not in_detail
            and (in_inspiration or self._document.root_id not in selected)))
        if not node:
            for name in ("add_child", "add_sibling", "insert_parent", "edit", "delete", "copy", "cut",
                         "fold", "expand_all", "collapse_all", "move_up", "move_down", "hyperlink"):
                self._actions[name].setEnabled(False)
        self._actions["create_submap"].setEnabled(bool(
            self.editable and not editing and single and node and not node.children and not node.submap_id
            and not in_detail and not image_only))
        self._actions["create_submap"].setToolTip("仅没有普通子节点和子导图的叶子节点可创建")
        self._actions["enter_submap"].setEnabled(bool(not editing and single and node and node.submap_id))
        self._actions["delete_submap"].setEnabled(bool(
            self.editable and not editing and single and node and node.submap_id))
        favorite = self._actions["favorite_submap"]
        favorite.setVisible(bool(single and node and node.submap_id))
        favorite.setEnabled(bool(self.editable and not editing and single and node and node.submap_id))
        checked = bool(node and self.engine.is_submap_favorite(node.submap_id))
        favorite.setText("取消收藏子导图" if checked else "收藏子导图")
        set_action_icon(favorite, "favorite_on" if checked else "favorite", self._ui_colors)
        # 创建入口按所在页面过滤，已有入口按当前节点过滤；返回其他页面后立即恢复。
        self._actions["create_submap"].setVisible(not in_detail and not image_only)
        self._actions["create_detail_page"].setVisible(
            not in_detail and not image_only and not bool(node and node.detail_page_id))
        self._actions["create_detail_page"].setEnabled(bool(
            self.editable and not editing and single and node and not node.detail_page_id
            and not in_detail and not image_only))
        for name in ("enter_detail_page", "delete_detail_page"):
            self._actions[name].setVisible(bool(not in_detail and node and node.detail_page_id))
            self._actions[name].setEnabled(bool(
                not editing and single and node and node.detail_page_id
                and (name == "enter_detail_page" or self.editable)))
        self._sync_navigation()

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

    def selected_node_ids(self) -> list[str]:
        """按树的先序顺序返回选择，移动后仍与文档层级一致。"""
        selected = {item.node.id for item in self.scene.selectedItems() if isinstance(item, NodeItem)}
        if len(selected) < 2:
            return list(selected)
        if self._incremental_layout is not None:
            return sorted(selected, key=self._incremental_layout.geometry.order)
        if self._node_order is None:
            self._node_order = {
                key: index for index, key in enumerate(self._document.ordered_nodes())
            }
        return sorted(selected, key=self._node_order.__getitem__)

    def current_node_id(self) -> str:
        """取得首个选中节点；无选择时使用导航临时显示的节点，再回退到当前页根节点。

        仅查询界面状态，不强制展开节点、不改变选择，也不创建历史记录。
        """
        ids = self.selected_node_ids()
        return ids[0] if ids else self.focus_id or self._document.root_id

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
        self.view.image_resize.cancel()
        self.view.viewport().update()
        if self._refreshing or self._disposed:
            return
        self._cancel_branch_menu()
        self._update_actions()
        self.view.update_cursor()
        self.selection_changed.emit(self.selected_node_ids())

    def _engine_changed(self, event):
        """引擎已提交而读模型尚未同步时，暂停路径查询，避免访问刚删除的旧选择。"""
        if self._disposed:
            return
        self._applying_engine_event = True
        try:
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

    def _apply_engine_event(self, event):
        if self._disposed:
            return
        external = self.is_external_event(event)
        if event.kind != "saved" and event.changes is not None and (
                self.current_page_kind == "inspiration" or "inspiration" in event.changes.settings):
            self._apply_inspiration_event(event)
            return
        # 在画布刷新引发选择信号之前通知宿主，防止旧侧栏输入被提交到新文档或撤销状态。
        if event.kind != "saved":
            self.view.image_resize.cancel()
            self.view.node_drag.document_changed()
            # 自动保存只更新保存标记，不触发文档替换通知，不打断菜单或未完成输入。
            self.document_changing.emit(event)
            self._cancel_branch_menu()
            self._cache_page_view()
            before = self.selected_node_ids()
            old_parents = {key: self._node_parents.get(key) for key in before}
            old_layout, old_theme = self._document.layout, self._document.theme
            if self.editor is not None:
                pair = (event.changes.collections.get("nodes", {}).get(self.editor.node_id)
                        if event.changes is not None else None)
                removed = pair is not None and pair[1 if event.forward else 0] is None
                # 已删除实体不能再查询页归属；reset 也无需对旧编辑节点做引擎查询。
                if (not external or event.changes is None or removed
                        or self.engine.node_page_id(self.editor.node_id) != self.current_page_id):
                    self.finish_edit(commit=False)
            changes = event.changes
            switched = False
            local_changes = None
            if changes is not None:
                index = 1 if event.forward else 0
                delta = changes.collections.get("nodes", {})
                changes.apply(self._book_document, forward=event.forward)
                target_page = self._current_page_id
                if event.kind in ("undo", "redo") and not external:
                    target_page = changes.page_id
                if not self._book_document.has_page(target_page):
                    target_page = next((page for page in reversed(self._page_chain)
                                        if self._book_document.has_page(page)), None)
                if target_page != self._current_page_id:
                    self._switch_page(target_page, notify=False, cache=False)
                    switched = True
                    before = self.selected_node_ids()
                    old_parents = {key: self._node_parents.get(key) for key in before}
                # 全局差量只合并属于当前页的节点；其他页的新增节点不能漏进画布或大纲。
                local_delta = {key: pair for key, pair in delta.items()
                               if key in self._document.nodes or (pair[index] is not None
                                   and self.engine.node_page_id(key) == self._current_page_id)}
                delta = local_delta
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
                for key, pair in delta.items():
                    if pair[index] is None:
                        self._document.nodes.pop(key, None)
                    else:
                        self._document.nodes[key] = self._book_document.nodes[key]
                source = self._book_document.page_record(self._current_page_id)
                self._document.layout, self._document.theme = source.layout, source.theme
                self._document.assets = self._book_document.assets
                self._document.metadata = self._book_document.metadata
                for key, pair in delta.items():
                    node = pair[index]
                    old = pair[1 - index]
                    if node is not None and (old is None or old.children != node.children):
                        for child in node.children:
                            self._node_parents[child] = key
                self._measure_dirty = None if old_theme != source.theme or switched else tuple(delta)
                if not switched:
                    settings = {}
                    for name, old, new in (("layout", old_layout, source.layout),
                                           ("theme", old_theme, source.theme)):
                        if old != new:
                            settings[name] = (old, new) if event.forward else (new, old)
                    local_changes = ChangeSet({"nodes": delta, "assets": changes.collections.get("assets", {})},
                                              settings, self._current_page_id)
            else:
                self._book_document = self.engine.document
                self._current_page_id = None
                self._inspiration_root_id = self._last_inspiration_root = None
                self._page_states.clear()
                self._page_chain = [None]
                self._view_restore_center = None
                self._view_reset_pending = False
                self._view_reset_timer.stop()
                self.view._hidden_center = None
                self._document = self._book_document.page_view()
                self._node_parents = self._document.parents()
                new_ids = []
                self._measure_dirty = None
                self._incremental_layout = None
            self._node_order = None
            after = [key for key in before if key in self._document.nodes]
            parents = self._node_parents
            if new_ids and not external:
                new_set = set(new_ids)
                after = [
                    key for key in new_ids if parents.get(key) not in new_set
                ]
            if not after and (before or not external):
                after = [old_parents[key] for key in before if old_parents.get(key) in self._document.nodes]
                after = after or [self._document.root_id]
            if event.kind == "reset":
                self.focus_id = None
                self._selection_undo.clear()
                self._selection_redo.clear()
                after = [self._document.root_id]
            elif event.kind == "undo" and self._selection_undo:
                selection = self._selection_undo.pop()
                self._selection_redo.append(selection)
                if not external:
                    after = selection[0]
            elif event.kind == "redo" and self._selection_redo:
                selection = self._selection_redo.pop()
                self._selection_undo.append(selection)
                if not external:
                    after = selection[1]
            elif event.kind == "change":
                self._selection_undo.append((before, after))
                del self._selection_undo[: -self.engine.history_limit]
                self._selection_redo.clear()
            if self.focus_id not in self._document.nodes:
                self.focus_id = None
            self.refresh()
            if self.editor is not None:
                self.editor.base_layout = self.layout_result
                self._update_edit_preview(self.editor, force=True)
            after = [key for key in after if key in self._document.nodes]
            self.select_nodes(after)
            event = ChangeEvent(event.operation, tuple(local_changes.node_ids) if local_changes else (),
                                event.revision, event.kind, local_changes, event.forward, event.source_id)
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
                self._incremental_layout = create_incremental_layout(self.layout_registry, document, sizes)
                self._incremental_key = key
            else:
                self._incremental_layout.update(document, sizes, dirty)
            self.layout_result = self._incremental_layout.result()
        else:
            self._incremental_layout = None
            self.layout_result = self.layout_registry.layout(document, sizes, focus_id=self.focus_id)
        # 完整布局只推导一次方向；增量路径直接复用几何索引，避免每个按钮遍历整图。
        self._fold_sides = (
            document.branch_sides() if self._incremental_layout is None and document.layout.direction == "both"
            else {}
        )
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
        if self.editor is not None:
            self.editor.sync_geometry(force=True)

    def _can_incremental(self):
        """由布局提供者判断优化能力；聚焦仍沿用完整布局，不绕过宿主注入入口。"""
        return (self.focus_id is None
                and self.current_page_kind != "inspiration"
                and supports_incremental_layout(self.layout_registry, self._document.layout.algorithm_id))

    def _fold_direction(self, node_id: str) -> str:
        """返回按钮伸出的方向；双向根节点固定向右，其他节点跟随所属一级分支。

        普通三种方向直接读取配置。双向增量画布沿用几何的祖先索引查询，聚焦或
        自定义布局复用 refresh 中一次生成的方向映射；悬停不重建方向或布局。
        """
        direction = self._document.layout.direction
        if direction != "both":
            return direction
        if node_id == self._document.root_id:
            return "right"
        if self._incremental_layout is not None:
            return self._incremental_layout.geometry.side(node_id)
        return self._fold_sides.get(node_id, "right")

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
            # 原地编辑只覆盖当前节点的展示视觉；正式尺寸和共享缓存仍保留最后
            # 一次提交的数据。虚拟化在滚动时重用图元，也必须保留这份草稿外观。
            visual = (self.editor.preview_visual
                      if self.editor is not None and key == self.editor.node_id else visuals[key])
            item = self.node_items.get(key)
            if item is None:
                item = NodeItem(document.nodes[key], visual, document.theme, self)
                self.scene.addItem(item)
                self.node_items[key] = item
            elif (
                item.visual is not visual
                or item.node.expanded != document.nodes[key].expanded
                or item.node.note != document.nodes[key].note
                or item.node.hyperlink != document.nodes[key].hyperlink
                or item.theme.accent != document.theme.accent
            ):
                item.update_content(document.nodes[key], visual, document.theme)
            else:
                # 视觉未变也同步实体引用，正文导航等读取当前快照，但不使 Qt 几何失效。
                item.node, item.theme = document.nodes[key], document.theme
            item.sync_fold_control()
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
        key = self.focus_id or self._document.root_id
        rect = (self.layout_result.bounds if self.current_page_kind == "inspiration"
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

    def begin_edit(self, node_id: str) -> None:
        """为当前页可见的普通节点开始正文编辑，返回 None。

        node_id 为节点标识；只读、不可见或纯图片节点直接返回，不提交已有草稿。
        成功时先提交原编辑器，再建立实时预览换行和尺寸的新编辑器；文字最终提交
        只生成一次引擎操作。提交错误向上传递。仅在 GUI 线程、画布存活时使用，
        编辑器由本画布和场景管理，调用方不得在结束编辑或切页后保留其引用。
        """
        if (not self.editable or self.is_composing or node_id not in self.layout_result.nodes
                or self._document.nodes[node_id].is_image_node):
            return
        self.finish_edit(commit=True)
        self.select_nodes([node_id])
        item = self.node_items[node_id]
        item.editing = True
        item.update()
        self.editor = NodeEditor(self, self.engine.get_node(node_id), item.visual)
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
            if self._incremental_layout is not None:
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
        """
        editor = self.editor
        if editor is None or (commit and editor.composing):
            return
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
            self.call(lambda: self.engine.update_node(node_id, text=text))
        self._update_actions()
        self.view.setFocus()
        self.view.update_cursor()
        self.editing_changed.emit(False)

    def edit_selected(self):
        """编辑当前节点的正文；具体只读与输入法处理由节点编辑器负责。"""
        self.begin_edit(self.current_node_id())

    @document_interaction
    def add_child(self):
        """为当前节点添加子节点并开始编辑正文，返回新节点标识。

        只读时拒绝操作；节点属性及分支继承遵循引擎规则，正文通过独立编辑会话提交。
        """
        self._ensure_editable()
        key = self.engine.add_child(self.current_node_id())
        self.begin_edit(key)
        return key

    @document_interaction
    def add_sibling(self):
        """在当前节点旁添加同级主题并开始正文编辑，返回新节点标识。

        同级顺序、方向继承及根节点限制统一由引擎校验，失败不保留部分结构修改。
        """
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
        """一次删除选择的节点子树，节点和图片资源共用一条撤销记录。"""
        self._ensure_editable()
        ids = self.selected_node_ids()
        if ids:
            nodes = {key for root in ids for key in self._book_document.owned_descendants(root)}
            submaps = sum(bool(self._book_document.nodes[key].submap_id) for key in nodes)
            details = sum(bool(self._book_document.nodes[key].detail_page_id) for key in nodes)
            if submaps or details:
                generation, revision = self.engine.document_generation, self.engine.revision
                choice = QMessageBox.question(
                    self, "删除节点及附属页面",
                    f"将删除 {len(nodes)} 个节点，以及其中的 {submaps} 张子导图、{details} 张详情页。\n删除后可以撤销。",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
                if choice != QMessageBox.StandardButton.Yes:
                    return
                self._ensure_editable()
                if self._disposed or (generation, revision) != (
                        self.engine.document_generation, self.engine.revision):
                    raise MindMapError("确认期间导图发生变化，请重新删除")
            self.engine.remove_nodes(ids)

    @document_interaction
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

    @document_interaction
    def cut(self):
        """先按注入格式复制当前选择，再删除相应子树；只读或选中根节点时拒绝。

        剪贴板编码失败时不会执行删除；正式删除使用引擎的历史与资源清理规则。
        """
        self._ensure_editable()
        if self.current_page_kind != "inspiration" and self._document.root_id in self.selected_node_ids():
            raise MindMapError("不能剪切根节点")
        self.copy()
        self.engine.remove_nodes(self.selected_node_ids())

    @document_interaction
    def paste(self) -> list[str]:
        """同步粘贴节点片段、图片或文本，返回本次应选中的主题标识列表。

        无参数；按项目片段、亿图片段、图像、文本的优先级解析，无可处理内容返回空列表。
        图片在编辑中的节点或 current_node_id() 下追加纯图片子节点，多选仅处理首个父节点。
        图片校验通过后才提交有效草稿；输入法候选、只读、失效目标或坏图抛出 MindMapError，
        不改变草稿、文档或历史。成功后选中新图片子节点，创建独立参与撤销、重做及保存。
        片段继续使用注入的格式注册表，返回新增标识并选中它们；格式及文件读取错误向上传递。
        文本按首个非空行自动识别 Markdown 或缩进大纲；缩进文本以 as_fragment=True
        委托注入的编解码器解析多根片段。忽略 BOM 和空白行后仅有一行、且解析片段
        仅含一个正文节点时，直接追加为子节点，不读取或更新共享偏好；Markdown
        仍按原规则去除标题或列表标记。多行或扩展导入器返回多节点时，完整校验后
        显示与亿图共用偏好的配置弹窗；目标定位沿用 current_node_id()。
        追加或替换共用一次撤销记录；替换时返回目标标识，追加时返回新增顶层标识。
        灵感区无选择时直接创建游离树，不显示选项或更新共享偏好。
        文本格式确定后不降级重试，缩进及层级错误包含原文行号，失败不改变选择或历史。
        仅在 GUI 线程、画布释放前调用；剪贴板载荷只在同步调用期间借用，不保留其所有权。
        界面入口通过 call() 将异常交给统一错误信号，普通文本编辑仍由文本控件处理。
        """
        self._ensure_editable()
        mime = QApplication.clipboard().mimeData()
        if mime is None:
            return []
        outline_title = None
        show_outline_options = True
        if mime.hasFormat(CLIPBOARD_MIME):
            fragment = decode_document(bytes(mime.data(CLIPBOARD_MIME)), registry=self.format_registry)
        elif mime.hasFormat(EDRAW_CLIPBOARD_MIME):
            # 先读完并验证结构，再询问放置方式；亿图解析失败时不能降级为纯文本。
            fragment = read_edraw_clipboard(bytes(mime.data(EDRAW_CLIPBOARD_MIME)))
            if self.current_page_kind != "inspiration" or self.selected_node_ids():
                return self._paste_edraw(fragment)
        elif has_clipboard_image(mime):
            return self._paste_image(mime)
        elif mime.hasText():
            text = mime.text()
            format_id = detect_outline_format(text)
            outline_title = "粘贴缩进大纲" if format_id == "text" else "粘贴 Markdown 大纲"
            document = decode_document(
                text.encode("utf-8"),
                registry=self.format_registry,
                format_id=format_id,
                options={"as_fragment": True} if format_id == "text" else None,
            )
            if format_id == "text":
                # 纯文本编码器已返回带合成根的片段，不能再次包装而增加可见容器节点。
                fragment = document
            else:
                fragment = MindMapDocument.new("剪贴板")
                fragment.root.children = [document.root_id]
                fragment.nodes.update(document.nodes)
            # 原文和解析结果必须同时满足单行单节点，避免把扩展导入器返回的树或森林
            # 当作普通文字直贴；仍通过注入的编解码器解析和校验，不自行创建正文节点。
            show_outline_options = not (
                sum(bool(line.strip()) for line in text.removeprefix("\ufeff").splitlines()) == 1
                and len(fragment.nodes) == 2
                and len(fragment.root.children) == 1
            )
        else:
            return []
        include_pages = True
        if self.current_page_kind == "detail" and (fragment.submaps or fragment.detail_pages):
            target_id = self.current_node_id()
            generation, revision = self.engine.document_generation, self.engine.revision
            # 明确说明只省略附属页面；关闭或取消不改写片段、当前选择或撤销历史。
            choice = QMessageBox.question(
                self, "粘贴到详情页",
                f"所复制的分支包含 {len(fragment.submaps)} 张子导图和 {len(fragment.detail_pages)} 张详情页。\n"
                "详情页内不能拥有附属页面。是否仅粘贴普通分支、文字、图片和样式？\n"
                "附属页面将不会粘贴，剪贴板内容保持完整。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if choice != QMessageBox.StandardButton.Yes:
                return []
            self._ensure_editable()
            if (self._disposed or (generation, revision) != (
                    self.engine.document_generation, self.engine.revision)
                    or self.current_page_kind != "detail" or self.current_node_id() != target_id):
                raise MindMapError("粘贴期间导图或目标已发生变化，请重新粘贴")
            include_pages = False
        if outline_title is not None and (self.current_page_kind != "inspiration" or self.selected_node_ids()):
            return self._paste_outline(
                fragment, title=outline_title, include_pages=include_pages, show_options=show_outline_options,
            )
        revision = self.engine.revision
        if self.current_page_kind == "inspiration" and not self.selected_node_ids():
            with self.engine.transaction("粘贴游离分支"):
                container = self.engine.add_inspiration("剪贴板临时容器", position=(0, 0))
                ids = self.engine.paste_fragment(container, fragment, include_pages=include_pages)
                candidate = self.engine.page_document(self.engine.inspiration_id)
                self.engine.move_to_inspiration(ids, positions=self.inspiration_positions(ids, candidate))
                self.engine.remove_nodes([container])
        else:
            ids = self.engine.paste_fragment(self.current_node_id(), fragment, include_pages=include_pages)
        self.select_nodes(ids)
        if self.engine.revision != revision:
            self._update_history_view_after()
        return ids

    def _paste_editor_image(self) -> bool:
        """尝试接管正文编辑器的图像粘贴，返回是否已经消费本次快捷键。

        无参数，仅由有效编辑器在 GUI 按键事件内同步调用。非图像或带专用节点片段时
        返回 False，保留文本控件原行为；图像返回 True，失败通过 call() 发出错误信号，
        避免坏图或输入法候选被误当成文本。MIME 对象仅借用到本次调用结束，不缓存。
        """
        mime = QApplication.clipboard().mimeData()
        if (mime is None or mime.hasFormat(CLIPBOARD_MIME) or mime.hasFormat(EDRAW_CLIPBOARD_MIME)
                or not has_clipboard_image(mime)):
            return False
        self.call(lambda: self._paste_image(mime))
        return True

    def _paste_image(self, mime: QMimeData) -> list[str]:
        """校验借用的图像载荷后创建子节点，返回单个新增标识或无图时的空列表。

        仅在 GUI 线程、组件存活期间同步执行。只读、候选输入、目标或文档失效及图片
        解码错误抛出 MindMapError；读取并验证图片前不提交任何草稿。有效草稿沿用各自
        历史，随后图片子节点独立提交；资源生命周期由引擎负责。调用不保留 MIME 引用。
        """
        self._ensure_editable()
        if self.current_page_kind == "inspiration" and not self.selected_node_ids() and self.editor is None:
            raise MindMapError("请先选择父主题，再粘贴图片")
        target_id = self.editor.node_id if self.editor is not None else self.current_node_id()
        page_id, generation = self.current_page_id, self.engine.document_generation

        def check_target():
            self._ensure_editable()
            if (self._disposed or generation != self.engine.document_generation
                    or page_id != self.current_page_id or target_id not in self._document.nodes):
                raise MindMapError("粘贴期间导图或目标已发生变化，请重新粘贴")
            if self.is_composing or not all(guard() for guard in tuple(self._navigation_guards)):
                raise MindMapError("请先完成输入法候选，再粘贴图片")
            self._check_image_parent(target_id)

        check_target()
        image = read_clipboard_image(mime)
        if image is None:
            return []
        check_target()
        return [self._insert_image_child(target_id, *image)]

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

    def _paste_edraw(self, fragment: MindMapDocument) -> list[str]:
        """兼容原亿图入口；委托共用流程收集选项并一次提交片段、选择和共享偏好。"""
        return self._paste_outline(fragment, title="粘贴亿图脑图")

    def _paste_outline(
        self, fragment: MindMapDocument, *, title: str, include_pages: bool = True,
        show_options: bool = True,
    ) -> list[str]:
        """按需询问外部大纲的放置方式，原子地追加或替换，返回应选中的主题标识。

        fragment 为带合成根的独立片段，先校验再弹窗；title 用于来源标题及事务说明。
        include_pages 沿用主粘贴入口的附属页面确认结果，False 时仅导入普通分支。
        show_options 默认 True，显示配置并使用共享偏好；False 仅由 paste 已确认的
        单行单节点文本使用，固定追加且不读取、修改偏好或发送偏好变更信号。
        单根允许替换目标正文或仅导入子树，原有后代可独立保留；多根仅允许追加。
        片段只在副本上继承目标分支样式，调用方的数据及系统剪贴板始终保持原样。
        取消返回空列表；只读、失效现场、非法选项或引擎拒绝抛出中文 MindMapError，
        本次操作不改变文档、选择、历史或偏好。确认后重新检查文档代次、修订、页面
        与目标；成功后以一次事务提交，并且只为经过配置确认的单根粘贴记忆共享偏好。
        无内容变化不新增撤销记录，但成功确认的单根选项仍可记忆。仅在 GUI 线程、
        画布有效且具有目标时调用；灵感区无选择的游离树创建由 paste 另行处理。
        """
        self._ensure_editable()
        fragment.validate()
        target_id = self.current_node_id()
        target = self.engine.get_node(target_id)
        page_id = self.current_page_id
        generation, revision = self.engine.document_generation, self.engine.revision
        roots = fragment.root.children
        if show_options:
            mode, keep_children, replace_root = self.edraw_paste_options
            choice = edraw_paste_options(
                self, target_text=target.text, root_count=len(roots), node_count=len(fragment.nodes) - 1,
                mode=mode, keep_children=keep_children, replace_root=replace_root,
                theme=self._ui_colors_override or self._document.theme, title=title,
            )
        else:
            # 单行直贴固定追加；不借用共享默认值，避免上一次替换选择覆盖目标或后代。
            choice = EdrawPasteOptions("append", True, True)
        if choice is None:
            return []
        self._ensure_editable()
        # 模态弹窗仍会处理宿主事件，即使未修改内容，切页或切换目标也会使本次选择失效。
        if self._disposed or (generation, revision) != (
            self.engine.document_generation, self.engine.revision
        ) or self.current_page_id != page_id or self.current_node_id() != target_id:
            raise MindMapError("粘贴期间导图、页面或目标已发生变化，请重新粘贴")
        if (choice.mode not in ("append", "replace") or not isinstance(choice.keep_children, bool)
                or not isinstance(choice.replace_root, bool)):
            raise MindMapError("粘贴选项无效")
        if choice.mode == "replace" and len(roots) != 1:
            raise MindMapError("多个并列主题只能追加到选中节点下")

        # 新主题沿用正常 add_child 的分支继承规则，字体、颜色、方向等保持项目默认值。
        # 每个节点分别深拷贝，避免之后编辑某条分支时连带改变其他新主题。
        fragment = deepcopy(fragment)
        if "branch" in target.style:
            for node in fragment.nodes.values():
                if node.id != fragment.root_id:
                    node.style["branch"] = deepcopy(target.style["branch"])
        with self.engine.transaction(title):
            if choice.mode == "replace":
                source = fragment.nodes[roots[0]]
                if not choice.keep_children:
                    self.engine.remove_nodes(target.children)
                # 保留根节点时完全跳过正文更新，保留文字格式，也避免触发附属页面的名称同步。
                # 根节点选择只影响目标正文；后代删除、导入仍由独立选项控制并共用一次事务。
                if choice.replace_root:
                    self.engine.update_node(target_id, text=source.text, text_format="plain")
                # paste_fragment 忽略片段根；把来源根作为合成根即可只插入它的后代。
                # 目标 ID、同级位置和本项目属性始终保留；保留的旧后代排在新后代之前。
                replacement = deepcopy(fragment)
                del replacement.nodes[replacement.root_id]
                replacement.root_id = source.id
                self.engine.paste_fragment(target_id, replacement, include_pages=include_pages)
                ids = [target_id]
            else:
                ids = self.engine.paste_fragment(target_id, fragment, include_pages=include_pages)
        self.select_nodes(ids)
        # 引擎通知默认选中新插入的后代；替换后应选中目标，重做时也必须恢复同一选择。
        if self.engine.revision != revision and self._selection_undo:
            before, _ = self._selection_undo[-1]
            self._selection_undo[-1] = (before, list(ids))
            self._update_history_view_after()
        # 仅在确认过配置的单树粘贴完整成功后记忆选择；单行直贴与多根均不改写偏好。
        if show_options and len(roots) == 1:
            self.set_edraw_paste_options(choice.mode, choice.keep_children, choice.replace_root)
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
        export_document(
            self.engine.page_document(self._current_page_id,
                                      inspiration_root_id=self.current_inspiration_root_id),
            path,
            layout_registry=self.layout_registry,
            expand_all=expand_all,
            scale=scale,
        )

    @document_interaction
    def set_layout(self, config: LayoutConfig) -> None:
        """在提交前试算新布局，缺失算法、非法参数或计算失败均不产生部分修改。"""
        self._ensure_editable()
        candidate = self.engine.page_document(self._current_page_id)
        candidate.layout = config
        self.validate_document(candidate)
        self.engine.set_layout(config, page_id=self._current_page_id)

    def validate_document(self, document: MindMapDocument) -> None:
        """替换前验证算法、图片、测量与布局结果，失败时当前引擎和画布不变。"""
        document.validate()
        # 完整文件装载时逐页校验；页内候选仍只测量当前页，禁止混排所有页面节点。
        pages = [document.page_view()] + [document.page_view(page.id) for page in document.iter_pages()]
        for page in pages:
            self.layout_registry.validate_config(page.layout)
            _, sizes = self.factory.measure(page)
            self.layout_registry.layout(page, sizes)

    def context_menu(self, global_position):
        """在指定全局位置显示复用内置及扩展 QAction 的右键菜单。

        动作状态及快捷键归画布统一管理；查看菜单本身不提交文档修改，关闭后释放菜单。
        """
        menu = QMenu(self)
        groups = [
            ("add_inspiration", "move_to_inspiration", "attach_inspirations", "group_inspirations"),
            ("add_child", "add_sibling", "insert_parent", "edit", "properties", "delete"),
            ("create_submap", "enter_submap", "favorite_submap", "delete_submap"),
            ("create_detail_page", "enter_detail_page", "delete_detail_page"),
            ("copy", "cut", "paste", "undo", "redo"),
            ("move_up", "move_down", "fold", "expand_all", "collapse_all"),
            ("image", "image_url", "remove_image", "hyperlink"),
            ("fit", "center"),
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
