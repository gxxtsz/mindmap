"""接入工作台的软件结构文档、连线筛选保存、标签复制和会话恢复。"""

from dataclasses import replace
from functools import partial
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction
from workbench_api import Disposable, EditorProvider, OperationResult, PluginContext, SavePolicy

from .document import SOFTWARE_STRUCTURE_SUFFIX, read_software_structure, rewrite_edge_filter
from .module_deps.canvas import ModuleDependencyCanvas
from .storage import write_atomic
from .template import DOWNLOAD_TEMPLATE_COMMAND_ID

_PROVIDER_ID = "software-structure.document"
MODULE_API_PANEL_ID = "software-structure.module-apis"


class SoftwareStructureDocument(QObject):
    """同一路径的标签共享模块、依赖和最近一次保存成功的连线筛选设置。"""

    save_policy = SavePolicy.READ_ONLY
    is_dirty = False
    # 每次文件提交成功后发送一次最新文档快照，供各个画布和侧栏统一回显。
    edge_filter_changed = Signal(object)

    def __init__(self, context: PluginContext, path: Path):
        """绑定绝对路径，读取用于显示的文档和自动保存所需的磁盘快照。"""
        super().__init__()
        self.id = uuid4().hex
        self._context = context
        self._path = path
        self.content, self._snapshot = read_software_structure(path)

    @property
    def path(self) -> Path:
        """返回用于最近文件和会话恢复的绝对路径。"""
        return self._path

    @property
    def resource_uri(self) -> str:
        """将已绑定的本地路径表示为文件 URI，供宿主识别资源。"""
        return self.path.as_uri()

    def save(self, *, save_as: bool = False, reason: str = "explicit") -> OperationResult:
        """通过宿主的只读策略处理通用保存动作，连线筛选由侧栏直接保存。"""
        return OperationResult.cancelled("软件结构的模块与连线为只读")

    def set_module_edges_hidden(self, module_id: str, hidden: bool) -> bool:
        """保存指定模块的勾选，hidden 为 True 时筛掉与它相连的入边和出边。

        module_id 按文档中的原始标识匹配，未知标识抛出 ValueError。成功或勾选未变
        时返回 True。保存失败时报告原因并返回 False，侧栏据最近成功的快照还原勾选。
        """
        if all(module.id != module_id for module in self.content.modules):
            raise ValueError(f"未找到模块 ID（{module_id!r}）")
        current = self.content.hidden_edge_module_ids
        updated = current | {module_id} if hidden else current - {module_id}
        return self._save_edge_filter(updated)

    def set_all_module_edges_hidden(self, hidden: bool) -> bool:
        """一次提交勾选全部模块（True）或清除全部勾选（False）的结果。

        孤立模块也参与批量勾选。保存成功后通知本文件的所有标签，失败时保留最近
        成功的快照并返回 False。勾选未变时直接返回 True。
        """
        updated = frozenset(module.id for module in self.content.modules) if hidden else frozenset()
        return self._save_edge_filter(updated)

    def _save_edge_filter(self, hidden_edge_module_ids: frozenset[str]) -> bool:
        """一次提交筛选和旧文件缺少的格式说明，成功后才替换快照并通知视图。

        提交前用打开或上次保存的字节检查外部修改。失败时保留最近成功的快照，
        让各视图继续显示上次保存的筛选结果。
        """
        if hidden_edge_module_ids == self.content.hidden_edge_module_ids:
            return True

        content = replace(self.content, hidden_edge_module_ids=hidden_edge_module_ids)
        try:
            snapshot = rewrite_edge_filter(self._snapshot, content)
            write_atomic(self.path, snapshot, expected=self._snapshot)
        except OSError as error:
            message = f"保存连线筛选失败：{error}"
            self._context.log(message, level="error", source=self.path.name, source_path=str(self.path))
            self._context.ui.show_message(message)
            return False

        self._snapshot = snapshot
        self.content = content
        self.edge_filter_changed.emit(content)
        return True


class SoftwareStructureView:
    """每个标签持有独立画布，共用同一个软件结构文档。"""

    def __init__(self, context: PluginContext, document: SoftwareStructureDocument, view_id: str | None):
        """创建浏览控件。恢复时使用宿主保存的标签标识，普通打开时生成新标识。"""
        self.id = view_id if view_id is not None else uuid4().hex
        self.document = document
        self._context = context
        self.widget = ModuleDependencyCanvas(document.content)
        self.edit_actions: dict[str, QAction] = {}
        self._apply_theme()
        self._theme_subscription: Disposable | None = context.ui.subscribe_theme(self._apply_theme)
        self.widget.viewport_changed.connect(self._view_state_changed)
        self.widget.module_clicked.connect(self._module_clicked)
        self.document.edge_filter_changed.connect(self.widget.apply_edge_filter)

    @property
    def title(self) -> str:
        """使用文件主名作为标签标题，宿主负责区分来自不同目录的同名文件。"""
        return self.document.path.stem

    @property
    def status_text(self) -> str:
        """为活动文档状态栏提供图规模。"""
        content = self.document.content
        return f"{len(content.modules)} 个模块 · {len(content.module_dependencies)} 条依赖"

    @property
    def toolbar_actions(self) -> tuple[QAction, ...]:
        """提供当前分屏标签栏右侧的动作。各视图与文件菜单共用宿主注册的命令。"""
        return (self._context.ui.action(DOWNLOAD_TEMPLATE_COMMAND_ID),)

    def prepare_input(self) -> bool:
        """勾选操作在当次事件中提交，标签切换或关闭时可直接放行。"""
        return True

    def capture_state(self) -> dict:
        """捕获本标签的缩放和位置，供复制和会话恢复使用。临时预览只在当前标签生效。"""
        return self.widget.capture_viewport()

    def restore_state(self, state: dict | None) -> None:
        """应用文件筛选，在画布具有实际尺寸后恢复视口，无效状态回退为全图适配。"""
        self.widget.restore_viewport(state)

    def duplicate(self) -> OperationResult:
        """将同一文档的独立画布插在源标签之后，从源标签当前视口开始浏览。"""
        try:
            return _register_view(self._context, self.document, state=self.capture_state(),
                                  after=self.id, remember=False)
        except Exception as error:
            message = f"复制软件结构标签失败：{error}"
            path = self.document.path
            self._context.log(message, level="error", source=path.name, source_path=str(path))
            self._context.ui.show_message(message)
            return OperationResult.failed(error, reported=True)

    def dispose(self) -> None:
        """幂等断开主题、视口、模块点击和文件筛选通知，由宿主销毁已登记的控件。"""
        if self._theme_subscription is not None:
            self._theme_subscription.dispose()
            self._theme_subscription = None
            self.widget.viewport_changed.disconnect(self._view_state_changed)
            self.widget.module_clicked.disconnect(self._module_clicked)
            self.document.edge_filter_changed.disconnect(self.widget.apply_edge_filter)

    def _apply_theme(self, *_):
        self.widget.apply_theme(self._context.ui.theme_color)

    def _view_state_changed(self) -> None:
        self._context.documents.view_state_changed(self.id)

    def _module_clicked(self, module_id: str | None) -> None:
        """先同步被点击的分屏，再按需显示侧栏，输入焦点留在当前画布。

        宿主的焦点通知可能在下一轮事件循环才到达。主动激活被点击的标签，让侧栏
        即使在首次点击另一分屏时也能读取正确的选择。空白点击只更新选择和活动标签。
        """
        if self._context.documents.active is not self:
            self._context.documents.activate_view(self.id)
        if module_id is not None and not self._context.ui.panel_visible(MODULE_API_PANEL_ID):
            self._context.ui.activate_panel(MODULE_API_PANEL_ID, focus=False)


def _register_view(context: PluginContext, document: SoftwareStructureDocument, *, view_id: str | None = None,
                   state: dict | None = None, after: str | None = None, remember: bool) -> OperationResult:
    """只登记完成初始化的视图。登记失败时释放候选控件和插件订阅。"""
    view = SoftwareStructureView(context, document, view_id)
    try:
        view.restore_state(state)
        context.documents.register_view(_PROVIDER_ID, view, after=after, remember=remember)
    except Exception:
        view.dispose()
        view.widget.deleteLater()
        raise
    return OperationResult(view_id=view.id)


def _open_document(context: PluginContext, path: Path, view_id: str | None, state: dict | None) -> OperationResult:
    """普通打开复用已有标签。会话恢复可为同一路径重建多个独立视口。"""
    try:
        source = path.resolve()
        existing = context.documents.find_path(source)
        if isinstance(existing, SoftwareStructureView):
            if view_id is None:
                context.documents.activate_view(existing.id)
                context.ui.remember_path(source)
                return OperationResult(view_id=existing.id)
            document = existing.document
        else:
            document = SoftwareStructureDocument(context, source)
        return _register_view(context, document, view_id=view_id, state=state, remember=view_id is None)
    except Exception as error:
        return OperationResult.failed(error)


def register_document_provider(context: PluginContext) -> Disposable:
    """登记单一软件结构文档入口，扩展名用于打开文件对话框和路径路由。

    提供者只打开既有文件。会话恢复通过同一打开回调传入标签标识和视口状态。
    注册句柄由插件上下文管理，可在激活失败或关闭时注销。
    """
    return context.editors.register(EditorProvider(
        id=_PROVIDER_ID,
        title="软件结构",
        extensions=(f".{SOFTWARE_STRUCTURE_SUFFIX}",),
        new_document=None,
        open_document=partial(_open_document, context),
        file_icon="software-structure",
    ))
