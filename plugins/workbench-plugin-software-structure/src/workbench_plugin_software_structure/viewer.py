"""接入工作台的只读软件结构文档、标签复制和会话恢复。"""

from functools import partial
from pathlib import Path
from uuid import uuid4

from PySide6.QtGui import QAction
from workbench_api import Disposable, EditorProvider, OperationResult, PluginContext, SavePolicy

from .document import SoftwareStructure, load_software_structure
from .module_deps.canvas import ModuleDependencyCanvas
from .template import DOWNLOAD_TEMPLATE_COMMAND_ID

_PROVIDER_ID = "software-structure.document"


class SoftwareStructureDocument:
    """同一路径的标签共享一份只读文档，内容和磁盘路径在打开后固定。"""

    save_policy = SavePolicy.READ_ONLY
    is_dirty = False

    def __init__(self, path: Path, content: SoftwareStructure):
        """绑定已解析的绝对文件路径和已校验的数据，生成本次会话的文档标识。"""
        self.id = uuid4().hex
        self._path = path
        self.content = content

    @property
    def path(self) -> Path:
        """返回用于最近文件和会话恢复的绝对路径。"""
        return self._path

    @property
    def resource_uri(self) -> str:
        """将已绑定的本地路径表示为文件 URI，供宿主识别资源。"""
        return self.path.as_uri()

    def save(self, *, save_as: bool = False, reason: str = "explicit") -> OperationResult:
        """为直接调用方返回明确的取消结果，保持磁盘文件和资源绑定。"""
        return OperationResult.cancelled("软件结构文档为只读")


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

    @property
    def title(self) -> str:
        """使用文件主名作为标签标题，宿主负责区分来自不同目录的同名文件。"""
        return self.document.path.stem

    @property
    def status_text(self) -> str:
        """为活动文档状态栏提供图规模和只读状态。"""
        content = self.document.content
        return f"{len(content.modules)} 个模块 · {len(content.module_dependencies)} 条依赖 · 只读"

    @property
    def toolbar_actions(self) -> tuple[QAction, ...]:
        """提供当前分屏标签栏右侧的动作。各视图与文件菜单共用宿主注册的命令。"""
        return (self._context.ui.action(DOWNLOAD_TEMPLATE_COMMAND_ID),)

    def prepare_input(self) -> bool:
        """只读画布可直接参与标签切换和关闭检查。"""
        return True

    def capture_state(self) -> dict:
        """捕获本标签的可序列化视口，供复制标签和工作台会话存储使用。"""
        return self.widget.capture_viewport()

    def restore_state(self, state: dict | None) -> None:
        """在画布具有实际尺寸后恢复视口，无效状态由画布回退为全图适配。"""
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
        """幂等断开主题订阅和视口通知，宿主负责已登记控件的销毁。"""
        if self._theme_subscription is not None:
            self._theme_subscription.dispose()
            self._theme_subscription = None
            self.widget.viewport_changed.disconnect(self._view_state_changed)

    def _apply_theme(self, *_):
        self.widget.apply_theme(self._context.ui.theme_color)

    def _view_state_changed(self) -> None:
        self._context.documents.view_state_changed(self.id)


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
            document = SoftwareStructureDocument(source, load_software_structure(source))
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
        extensions=(".software-structure",),
        new_document=None,
        open_document=partial(_open_document, context),
        file_icon="software-structure",
    ))
