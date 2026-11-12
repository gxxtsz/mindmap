"""接入工作台的软件结构文档、阈值自动保存、标签复制和会话恢复。"""

from dataclasses import replace
from functools import partial
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QAction
from workbench_api import Disposable, EditorProvider, OperationResult, PluginContext, SavePolicy

from .document import (
    PublicDependencyThresholds,
    read_software_structure,
    rewrite_public_dependency_thresholds,
)
from .module_deps.canvas import ModuleDependencyCanvas
from .storage import write_atomic
from .template import DOWNLOAD_TEMPLATE_COMMAND_ID

_PROVIDER_ID = "software-structure.document"


class SoftwareStructureDocument(QObject):
    """同一路径的标签共享模块、依赖和最近一次保存成功的阈值。"""

    save_policy = SavePolicy.READ_ONLY
    is_dirty = False
    thresholds_changed = Signal(object)
    threshold_save_failed = Signal()

    def __init__(self, context: PluginContext, path: Path):
        """绑定绝对路径，读取用于显示的文档和自动保存所需的磁盘快照。"""
        super().__init__()
        self.id = uuid4().hex
        self._context = context
        self._path = path
        self.content, self._snapshot = read_software_structure(path)
        self._pending_thresholds: PublicDependencyThresholds | None = None
        self._threshold_timer = QTimer(self)
        self._threshold_timer.setSingleShot(True)
        self._threshold_timer.setInterval(250)
        self._threshold_timer.timeout.connect(self.flush_public_dependency_thresholds)

    @property
    def path(self) -> Path:
        """返回用于最近文件和会话恢复的绝对路径。"""
        return self._path

    @property
    def resource_uri(self) -> str:
        """将已绑定的本地路径表示为文件 URI，供宿主识别资源。"""
        return self.path.as_uri()

    def save(self, *, save_as: bool = False, reason: str = "explicit") -> OperationResult:
        """通过宿主的只读策略处理通用保存动作，阈值通过侧栏独立提交。"""
        return OperationResult.cancelled("软件结构的模块与连线为只读")

    def queue_public_dependency_thresholds(self, thresholds: PublicDependencyThresholds) -> None:
        """合并短时间内的连续输入，待保存值与当前文档绑定。"""
        if thresholds == self.content.public_dependency_thresholds:
            self._pending_thresholds = None
            self._threshold_timer.stop()
        else:
            self._pending_thresholds = thresholds
            self._threshold_timer.start()

    def flush_public_dependency_thresholds(self) -> None:
        """立即提交排队的阈值。成功后将新快照广播给同文件的各个标签。"""
        self._threshold_timer.stop()
        thresholds, self._pending_thresholds = self._pending_thresholds, None
        if thresholds is None:
            return

        try:
            snapshot = rewrite_public_dependency_thresholds(self._snapshot, thresholds)
            write_atomic(self.path, snapshot, expected=self._snapshot)
        except OSError as error:
            # 文件提交失败时，侧栏从文档取回上一次成功值，画布继续使用该快照。
            self.threshold_save_failed.emit()
            message = f"保存公共依赖阈值失败：{error}"
            self._context.log(message, level="error", source=self.path.name, source_path=str(self.path))
            self._context.ui.show_message(message)
            return

        self._snapshot = snapshot
        self.content = replace(self.content, public_dependency_thresholds=thresholds)
        self.thresholds_changed.emit(self.content)


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
        self.widget.dependency_view_changed.connect(self._view_state_changed)
        self.document.thresholds_changed.connect(self.widget.apply_public_dependency_thresholds)

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
        """宿主切换或关闭标签前，立即保存该文档在侧栏中的有效输入。"""
        self.document.flush_public_dependency_thresholds()
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
            self.document.flush_public_dependency_thresholds()
            return _register_view(self._context, self.document, state=self.capture_state(),
                                  after=self.id, remember=False)
        except Exception as error:
            message = f"复制软件结构标签失败：{error}"
            path = self.document.path
            self._context.log(message, level="error", source=path.name, source_path=str(path))
            self._context.ui.show_message(message)
            return OperationResult.failed(error, reported=True)

    def dispose(self) -> None:
        """幂等断开主题、视图状态和阈值通知，宿主负责已登记控件的销毁。"""
        if self._theme_subscription is not None:
            self._theme_subscription.dispose()
            self._theme_subscription = None
            self.widget.viewport_changed.disconnect(self._view_state_changed)
            self.widget.dependency_view_changed.disconnect(self._view_state_changed)
            self.document.thresholds_changed.disconnect(self.widget.apply_public_dependency_thresholds)

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
        extensions=(".software-structure",),
        new_document=None,
        open_document=partial(_open_document, context),
        file_icon="software-structure",
    ))
