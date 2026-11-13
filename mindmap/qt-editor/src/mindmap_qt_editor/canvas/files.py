"""提供原生导图打开、保存和当前页导出的宿主无关控制器。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from mindmap_core import FormatRegistration, MindMapDocument, MindMapEngine, MindMapError
from mindmap_core.formats import load_document, load_validated_document, save_document, supports_owned_load
from mindmap_core.formats.files import FileVersion, FileVersionConflict, check_file_version, file_version
from mindmap_core.layouts import supports_incremental_layout
from mindmap_core.node_pages import GRAPH_PAGE_TYPE
from mindmap_qt_render.rendering import VisualFactory
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QFileDialog, QMessageBox

from . import loading
from .widget import MindMapWidget

_REPLACE_DOCUMENT = MindMapEngine.replace_document
_ADOPT_DOCUMENT = MindMapEngine.adopt_document
_VALIDATE_DOCUMENT = MindMapWidget.validate_document
_MEASURE_DOCUMENT = VisualFactory.measure
_LOAD_DOCUMENT = load_document
_SAVE_DOCUMENT = save_document


class PendingEdits(Protocol):
    """文件操作前提交面板输入的最小协议，不要求安装面板或工作区包。

    独立画布宿主可以传入提供同名方法的对象；没有侧栏时该方法可以为空实现。
    有输入法候选或无效字段时，遵循宿主原有处理，禁止强制提交尚未完成的文字。
    """

    def flush_pending_edits(self) -> None:
        """同步提交已完成的有效输入；返回后文件控制器再结束画布文字编辑。"""
        ...


@dataclass
class MindMapFileState:
    """多个文件控制器共用的保存位置、磁盘版本及冲突标志。

    同一文档的视图传入同一实例；控制器仅在实际写入成功后更新 path，其他视图
    立即读取新路径。宿主负责统一保存调度和状态通知，不能把不同引擎绑定到同一
    实例。未显式传入时，每个控制器创建独立状态，保持原有单画布用法。
    version 由已打开或实际写入的文件建立，冲突持续到重新加载或成功写入为止。
    graph_filter_cleanup_pending 表示磁盘文件仍有旧图筛选，供干净文档的显式保存使用。
    """

    path: Path | None = None
    version: FileVersion | None = field(default=None, init=False)
    conflict: bool = field(default=False, init=False)
    graph_filter_cleanup_pending: bool = field(default=False, init=False)


class MindMapFileController(QObject):
    """绑定一个画布和面板组合，不拥有画布、不读写应用偏好。

    dialog_parent 是文件和保存对话框的 QWidget 父对象；panels 提供
    flush_pending_edits()。writer 默认为引擎原子写入方法，可注入兼容的
    写入器。open_path 的校验异常交给宿主处理，save 写入失败
    通过 error_occurred 报告并返回 False；取消操作保持原文档和保存状态。
    """

    state_changed = Signal()
    message = Signal(str)
    error_occurred = Signal(str)
    # 打开和保存只使用原生容器；额外编解码器仍供剪贴板等内存交换使用。
    OPEN_FILTER = "思维导图 (*.mindmap)"

    def __init__(self, canvas, panels: PendingEdits, dialog_parent=None, *, writer=save_document,
                 state: MindMapFileState | None = None, background_open: bool = False,
                 versioned_writer: bool = False):
        """绑定视图与写入器，可通过 state 共享路径；状态实例由调用方和控制器共同持有。

        省略 state 时行为与原接口一致；共享状态不自动共享引擎或提交其他视图输入，
        这些由宿主协调。所有方法及信号在 GUI 线程执行。

        background_open=True 时大于 1 MiB 的标准原生文件在工作线程解析及校验，等待
        期间 GUI 继续处理事件并提供取消；返回值仍表示本次打开已完成。普通文件和
        扩展读取器保持同步，后台线程不访问画布、面板或引擎，取消不提交候选。
        versioned_writer=True 表示初始注入的写入器支持与 save_document 相同的
        expected_version 参数及返回值，负责在实际替换文件前再次核验版本。
        其他自定义写入器沿用原签名，由控制器在调用前检查已绑定文件。
        """
        super().__init__(dialog_parent)
        self.canvas, self.engine, self.panels = canvas, canvas.engine, panels
        self.dialog_parent = dialog_parent
        self.writer = writer
        self._versioned_writer = writer if versioned_writer else _SAVE_DOCUMENT
        self.state = state if state is not None else MindMapFileState()
        self._background_open = background_open
        self._opening = False
        # 宿主如有后台写入，可注入 GUI 线程等待边界；路径切换和显式保存先收束旧写入。
        self.wait_for_pending_write = None
        # 多视图宿主可在显式保存前统一检查候选并提交所有关联标签；False 取消保存。
        self.prepare_save = None

    @property
    def path(self) -> Path | None:
        """返回共享保存位置；查询不访问磁盘，也不改变文件状态。"""
        return self.state.path

    @path.setter
    def path(self, value: Path | None) -> None:
        """设置所属文档的共享路径；宿主初始化可赋值，保存流程仅在写入成功后赋值。"""
        if self.state.path != value:
            self.state.path = value
            self.state.version = None
            self.state.conflict = False
            self.state.graph_filter_cleanup_pending = False

    @property
    def is_dirty(self) -> bool:
        """只读返回引擎修改状态；原生导图打开不再产生待另存的导入状态。"""
        return self.engine.is_dirty

    def report_error(self, error: str | Exception):
        """通知宿主写入失败。磁盘冲突保留在共享文件状态中，信号继续传递中文文本。"""
        if isinstance(error, FileVersionConflict):
            self.state.conflict = True
        self.error_occurred.emit(str(error))

    def format_filters(self, purpose: str) -> str:
        """打开与保存均返回唯一的原生格式筛选，其他用途抛出 MindMapError。

        不枚举注册表，不解析对话框返回的格式名称；额外完整格式也不能扩展
        文件打开或保存范围。通用编解码器继续服务剪贴板和底层内存接口。
        """
        if purpose in ("open", "save"):
            return self.OPEN_FILTER
        raise MindMapError("未知文件对话框用途")

    def require_native_save_format(self) -> FormatRegistration:
        """返回可完整读写的原生格式注册项，供同步及后台保存共用。

        不读取或写入文件、不提交输入、不改变保存位置及引擎状态。原生格式缺失、
        仅能导入或仅能导出时抛出中文 MindMapError，禁止回退到其他编解码器。
        调用方只读检查返回项；后台能力仍需根据编码器自身的公开契约决定。
        """
        try:
            entry = self.canvas.format_registry.get("mindmap")
        except MindMapError as exc:
            raise MindMapError("未注册可用的 .mindmap 原生编解码器") from exc
        if not entry.can_save:
            raise MindMapError("未注册可用的 .mindmap 原生编解码器")
        return entry

    def confirm_replace(self) -> bool:
        """提交已完成输入并询问保存；取消或保存失败返回 False，不释放任何控件。"""
        self.panels.flush_pending_edits()
        self.canvas.finish_edit(commit=True)
        if not self.engine.is_dirty:
            return True
        choice = QMessageBox.question(
            self.dialog_parent,
            "保存修改",
            "当前文档有未保存的修改，是否先保存？",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if choice == QMessageBox.StandardButton.Cancel:
            return False
        return self.save() if choice == QMessageBox.StandardButton.Save else True

    def new_document(self):
        """用户允许替换后建立空白文档；返回是否完成，取消时保持文件状态。"""
        if self.confirm_replace():
            if self.wait_for_pending_write is not None:
                self.wait_for_pending_write()
            self.path = None
            self.engine.replace_document(MindMapDocument.new())
            self.canvas.reset_view()
            self.state_changed.emit()
            return True
        return False

    def open_dialog(self):
        """只显示原生导图筛选；取消不提交输入或修改当前文档状态。"""
        path, _ = QFileDialog.getOpenFileName(
            self.dialog_parent, "打开导图", "", self.OPEN_FILTER
        )
        if path:
            return self.open_path(path)
        return False

    @staticmethod
    def validate_open_path(path: str | Path) -> Path:
        """统一验证文件扩展名，供对话框、会话与命令行在分配页面前使用。

        扩展名比较忽略大小写；禁止通过“所有文件”、手工输入或显式格式参数绕过。
        此处不读取文件、不提交输入，拒绝时保留当前文档和界面状态。
        """
        source = Path(path)
        if source.suffix.lower() != ".mindmap":
            raise MindMapError("仅支持打开 .mindmap 文件")
        return source

    def open_path(self, path: str | Path):
        """只使用原生容器解码，内容与画布校验成功后才询问替换并绑定原路径。

        不再接受导入格式选择；将 JSON 或文本改名为 .mindmap 也必须通过原生
        容器验证，不能通过扩展名绕过内容校验。失败不修改当前文档或保存标记。
        """
        return self._open(self.validate_open_path(path))

    def reload(self) -> bool:
        """用磁盘最新内容替换当前文件。调用方须先检查关联视图并确认放弃本地修改。

        沿用原生加载、后台取消及完整画布预检。任何失败都保留旧文档与磁盘版本。
        """
        return self.path is not None and self._open(self.validate_open_path(self.path), replacing=True)

    def _open(self, source, *, replacing=False):
        if self._opening:
            return False
        self._opening = True
        try:
            return self._open_path(source, replacing=replacing)
        finally:
            self._opening = False

    def _open_path(self, source, *, replacing):
        """在禁止同一控制器重入的边界内准备候选，全部预检成功后才提交。"""
        registry = self.canvas.format_registry
        # 宿主替换通用加载入口时继续调用它，避免原生优化绕过注入的读取调度。
        owned = load_document is _LOAD_DOCUMENT and supports_owned_load(registry)
        try:
            version = file_version(source)
        except OSError as exc:
            raise MindMapError(f"读取文档失败：{exc}") from exc
        background = (owned and self._background_open and version is not None
                      and version.size > loading.BACKGROUND_OPEN_BYTES)
        if background:
            validation = loading.load_native_in_background(source, self.dialog_parent)
            if validation is None:
                return False
        else:
            validation = load_validated_document(source, registry=registry) if owned else None
        document = validation.document if validation is not None else load_document(
            source, registry=registry, format_id="mindmap")
        # 只有标准只读预检可以延续加载凭据；扩展可能修改候选，仍须逐层重新校验。
        reuse_validation = (
            validation is not None and type(self.canvas) is MindMapWidget
            and getattr(self.canvas.validate_document, "__func__", None) is _VALIDATE_DOCUMENT
            and type(self.canvas.factory) is VisualFactory
            and getattr(self.canvas.factory.measure, "__func__", None) is _MEASURE_DOCUMENT
            and all(supports_incremental_layout(self.canvas.layout_registry, record.layout.algorithm_id)
                    for record in (document, *document.iter_pages(), document.inspiration)))
        if reuse_validation:
            self.canvas.validate_document(document, defer_inactive_pages=True, validation=validation)
        else:
            validation = None
            self.canvas.validate_document(document, defer_inactive_pages=True)
        if not replacing and not self.confirm_replace():
            return False
        if self.wait_for_pending_write is not None:
            self.wait_for_pending_write()
        try:
            check_file_version(source, version)
        except (FileVersionConflict, OSError) as exc:
            raise MindMapError(f"读取文档失败：{exc}，请重试") from exc
        cleanup_pending = any(record["data"]["hidden_node_ids"] for node in document.nodes.values()
                              if (record := node.extension_pages.get(GRAPH_PAGE_TYPE)) is not None)
        if (owned and type(self.engine) is MindMapEngine
                and getattr(self.engine.replace_document, "__func__", None) is _REPLACE_DOCUMENT):
            # 内置解码结果尚未发布，校验只读使用后直接交给引擎；扩展读取或引擎
            # 覆写仍走原有隔离入口，不把扩展拥有的实体借给正式编辑状态。
            if (validation is not None
                    and getattr(self.engine.adopt_document, "__func__", None) is _ADOPT_DOCUMENT):
                self.engine.adopt_document(document, validation=validation)
            else:
                self.engine.adopt_document(document)
        else:
            self.engine.replace_document(document)
        self.accept_file_version(source, version, graph_filter_cleanup_pending=cleanup_pending)
        # 初始视图使用正常倍率；由画布等待实际显示后居中，避免大型导图被整图
        # 适应压缩到无法阅读，也避免在会话安装前使用隐藏视口的占位尺寸。
        self.canvas.reset_view()
        return True

    @staticmethod
    def validate_save_path(path: str | Path) -> Path:
        """补齐缺失的原生扩展名；其他扩展名直接拒绝，不改写调用方路径或文件。

        大小写不同的 .mindmap 扩展名保持原样。此处只返回目标路径，实际写入前
        还会检查原生编解码能力及会话占用，成功后才更新控制器的绑定路径。
        """
        target = Path(path)
        if not target.suffix:
            return target.with_suffix(".mindmap")
        if target.suffix.lower() != ".mindmap":
            raise MindMapError("仅支持保存为 .mindmap 文件")
        return target

    def save(self, *, save_as=False):
        """提交有效输入并保存原生导图；成功才更新文件路径与已保存标记。

        对话框固定为 .mindmap，忽略返回的格式名称；缺少原生编解码器时在弹窗前
        报告失败。取消不改变绑定路径，错误扩展名不会触发写入或静默改名。
        """
        if self.prepare_save is not None and not self.prepare_save():
            return False
        self.panels.flush_pending_edits()
        self.canvas.finish_edit(commit=True)
        try:
            self.require_native_save_format()
        except MindMapError as exc:
            self.report_error(str(exc))
            return False
        target = self.path
        if save_as or target is None:
            filename, _ = QFileDialog.getSaveFileName(
                self.dialog_parent,
                "保存导图",
                str(target or "我的思维导图.mindmap"),
                self.format_filters("save"),
            )
            if not filename:
                return False
            target = Path(filename)
        return self._write_snapshot(target)

    def save_committed(self, *, explicit: bool = False) -> bool:
        """只保存已提交快照，不弹出选择框、不结束原地编辑或提交侧栏草稿。

        自动保存调用此边界；保存通知仅更新历史的保存位置，不产生新的编辑记录。
        无绑定路径时报告错误并返回 False。即使快照没有修改，也检查路径、原生
        编解码器和磁盘版本，避免文件丢失或发生外部修改时被当作保存成功。
        explicit 为真时，磁盘上仍有旧图筛选的干净文档也会写入一次，清空兼容字段。
        """
        if self.path is None:
            self.report_error("导图尚未选择保存位置")
            return False
        needs_cleanup = explicit and self.state.graph_filter_cleanup_pending
        return self._write_snapshot(self.path, only_if_dirty=not needs_cleanup)

    def restore_missing(self) -> bool:
        """将已提交内容恢复到原路径。写入前再次要求该路径为空，防止覆盖重现的文件。"""
        return self.path is not None and self._write_snapshot(self.path, restore_missing=True)

    def expected_write_version(self, target: Path) -> FileVersion | None:
        """返回写入目标的预期版本。原路径须匹配上次打开或保存，另存目标取当前版本。"""
        if self.state.version is not None and self._is_bound_target(target):
            check_file_version(target, self.state.version)
            return self.state.version
        return file_version(target)

    def _is_bound_target(self, target: Path) -> bool:
        return self.path is not None and (target == self.path or target.resolve() == self.path.resolve())

    def accept_file_version(
        self, target: Path, version: FileVersion | None, *, graph_filter_cleanup_pending: bool = False,
    ) -> None:
        """记录已打开或写入的版本，解除共享文件的冲突并发出状态变更通知。

        只有加载了非空旧筛选的文件时传入 graph_filter_cleanup_pending=True。
        后台写入返回实际提交的版本，由宿主先更新引擎的保存快照再调用此方法。
        """
        self.state.path, self.state.version, self.state.conflict = target, version, False
        self.state.graph_filter_cleanup_pending = graph_filter_cleanup_pending
        self.state_changed.emit()

    def _write_snapshot(self, target, *, only_if_dirty=False, restore_missing=False):
        """手动、自动及首次保存共用原生路径检查和原子写入，失败不更新绑定路径。

        自动保存的干净快照可跳过写盘，但不能跳过路径与格式检查。写入器始终接收
        明确的 mindmap 标识和补齐后的路径，因此扩展格式及路径占用检查无法绕过。
        """
        try:
            target = self.validate_save_path(target)
            self.require_native_save_format()
            if self.wait_for_pending_write is not None:
                self.wait_for_pending_write()
            if restore_missing:
                check_file_version(target, None)
                expected = None
            else:
                expected = self.expected_write_version(target)
            if only_if_dirty and not self.is_dirty:
                self.state.conflict = False
                return True
            options = {"registry": self.canvas.format_registry, "format_id": "mindmap"}
            if self.writer == self._versioned_writer:
                options["expected_version"] = expected
            version = self.writer(self.engine.document, target, **options)
            if not isinstance(version, FileVersion):
                version = file_version(target)
        except FileVersionConflict as exc:
            # 另存目标被修改时保留原绑定文件的冲突状态，允许稍后继续保存原文件。
            self.report_error(exc if self._is_bound_target(target) else str(exc))
            return False
        except (MindMapError, OSError) as exc:
            self.report_error(exc)
            return False
        self.state.path = target
        self.engine.mark_saved()
        self.accept_file_version(target, version)
        return True

    def export_dialog(self, format_id: str = "svg"):
        """提交有效输入，为 png、svg、pdf 或 txt 打开单格式保存对话框。

        默认使用 svg。图形在选定文件后询问折叠范围。txt 读取当前页的完整快照，
        选定文件后直接写入，空页抛出 MindMapError。无扩展名时补齐所选格式，
        其他扩展名直接报错。取消时返回，文件写入成功后发出 message 信号。
        """
        formats = {"png": "PNG 图片", "svg": "SVG 矢量图", "pdf": "PDF 文档", "txt": "TXT 文本"}
        if not isinstance(format_id, str) or format_id not in formats:
            raise MindMapError("支持的导出格式为 PNG、SVG、PDF、TXT")
        is_graph = self.canvas.graph is not None
        if is_graph and format_id == "txt":
            raise MindMapError("可在“编辑图文本”中复制图的文本内容")
        label, extension = formats[format_id], "." + format_id
        self.panels.flush_pending_edits()
        self.canvas.finish_edit(commit=True)
        text_page = None
        if format_id == "txt":
            # 在打开文件选择框前固定页面范围。灵感区用当前想法的根限定快照，
            # 普通页面只读取自身分支，快照包含折叠节点及屏外节点。
            text_page = self.engine.page_document(
                self.canvas.current_page_id, inspiration_root_id=self.canvas.current_inspiration_root_id,
            )
            if not text_page.root_ids:
                raise MindMapError("暂无可导出的想法内容")
        path, _ = QFileDialog.getSaveFileName(
            self.dialog_parent, "导出 " + label, "思维导图" + extension, f"{label} (*{extension})"
        )
        if not path:
            return
        target = Path(path)
        if not target.suffix:
            target = target.with_suffix(extension)
        elif target.suffix.lower() != extension:
            # 不静默改写用户输入的另一个文件名，防止绕过保存对话框的覆盖确认。
            raise MindMapError(f"已选择{label}，请使用 {extension} 扩展名")
        if text_page is not None:
            # TXT 菜单使用约定的内置文本编码器，复用通用文件入口的临时写入与原子替换。
            save_document(text_page, target, format_id="text", options={"tab_indent": True})
        elif is_graph:
            self.canvas.export(target)
        else:
            expand = QMessageBox.question(
                self.dialog_parent,
                "导出范围",
                "是否展开全部节点后导出？\n选择“否”将保留当前折叠状态。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            self.canvas.export(target, expand_all=expand == QMessageBox.StandardButton.Yes)
        self.message.emit(f"已导出：{target}")

