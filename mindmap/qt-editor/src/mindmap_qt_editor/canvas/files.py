"""提供原生导图打开、保存和当前页导出的宿主无关控制器。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from mindmap_core import MindMapDocument, MindMapEngine, MindMapError
from mindmap_core.formats import (
    DirectoryVersion,
    DirectoryVersionConflict,
    check_directory_version,
    directory_size,
    directory_version,
    load_validated_directory,
    save_directory,
    save_document,
)
from mindmap_core.layouts import supports_incremental_layout
from mindmap_qt_render.rendering import VisualFactory
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

from . import loading
from .widget import MindMapWidget

_REPLACE_DOCUMENT = MindMapEngine.replace_document
_ADOPT_DOCUMENT = MindMapEngine.adopt_document
_VALIDATE_DOCUMENT = MindMapWidget.validate_document
_MEASURE_DOCUMENT = VisualFactory.measure
_SAVE_DIRECTORY = save_directory


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
    version 由已打开或实际写入的目录建立，冲突持续到重新加载或成功写入为止。
    """

    path: Path | None = None
    version: DirectoryVersion | None = field(default=None, init=False)
    conflict: bool = field(default=False, init=False)


class MindMapFileController(QObject):
    """绑定一个画布和面板组合，不拥有画布、不读写应用偏好。

    dialog_parent 是文件和保存对话框的 QWidget 父对象；panels 提供
    flush_pending_edits()。writer 默认为导图目录保存接口，可注入兼容的
    写入器。open_path 的校验异常交给宿主处理，save 写入失败
    通过 error_occurred 报告并返回 False；取消操作保持原文档和保存状态。
    """

    state_changed = Signal()
    message = Signal(str)
    error_occurred = Signal(str)
    def __init__(self, canvas, panels: PendingEdits, dialog_parent=None, *, writer=save_directory,
                 state: MindMapFileState | None = None, background_open: bool = False,
                 versioned_writer: bool = False):
        """绑定视图与写入器，可通过 state 共享路径；状态实例由调用方和控制器共同持有。

        省略 state 时行为与原接口一致；共享状态不自动共享引擎或提交其他视图输入，
        这些由宿主协调。所有方法及信号在 GUI 线程执行。

        background_open=True 时大于 1 MiB 的导图目录在工作线程解析及校验，等待
        期间 GUI 继续处理事件并提供取消；返回值仍表示本次打开已完成。后台线程
        不访问画布、面板或引擎，取消不提交候选。
        versioned_writer=True 表示初始注入的写入器遵循 save_directory 的
        expected_version 及返回值契约，负责在提交前再次核验版本。其他写入器接收
        文档和目标路径，由控制器先检查已绑定目录。
        """
        super().__init__(dialog_parent)
        self.canvas, self.engine, self.panels = canvas, canvas.engine, panels
        self.dialog_parent = dialog_parent
        self.writer = writer
        self._versioned_writer = writer if versioned_writer else _SAVE_DIRECTORY
        self.state = state if state is not None else MindMapFileState()
        self._background_open = background_open
        self._opening = False
        # 宿主如有后台写入，可注入 GUI 线程等待边界；路径切换和显式保存先收束旧写入。
        self.wait_for_pending_write = None
        # 多视图宿主可在显式保存前统一检查候选并提交所有关联标签；False 取消保存。
        self.prepare_save = None
        # 宿主在准备覆盖或写入前检查目标是否归其他会话持有。
        self.check_save_target = None

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

    @property
    def is_dirty(self) -> bool:
        """只读返回引擎修改状态；原生导图打开不再产生待另存的导入状态。"""
        return self.engine.is_dirty

    def report_error(self, error: str | Exception):
        """通知宿主写入失败。磁盘冲突保留在共享文件状态中，信号继续传递中文文本。"""
        if isinstance(error, DirectoryVersionConflict):
            self.state.conflict = True
        self.error_occurred.emit(str(error))

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
        """选择已有导图目录；取消不提交输入或修改当前文档状态。"""
        path = QFileDialog.getExistingDirectory(self.dialog_parent, "打开导图目录")
        if path:
            return self.open_path(path)
        return False

    @staticmethod
    def validate_open_path(path: str | Path) -> Path:
        """在分配编辑视图前检查路径是目录，解析相对路径及目录别名。"""
        source = Path(path)
        if not source.is_dir():
            raise MindMapError(f"请选择已有的思维导图目录：{source}")
        return source.resolve()

    def open_path(self, path: str | Path):
        """读取原生目录，内容与画布校验成功后才询问替换并绑定目录。"""
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
        # 只用总大小选择打开方式。清单缺失也交给目录读取器处理未完成的写入。
        try:
            size = directory_size(source) if self._background_open else None
        except (MindMapError, OSError):
            size = None
        if size is not None and size > loading.BACKGROUND_OPEN_BYTES:
            result = loading.load_native_in_background(source, self.dialog_parent)
            if result is None:
                return False
            validation, version = result
        else:
            validation, version = load_validated_directory(source)
        document = validation.document
        # 只有标准只读预检可以延续加载凭据；扩展可能修改候选，仍须重新校验。
        reuse_validation = (
            type(self.canvas) is MindMapWidget
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
            check_directory_version(source, version)
        except DirectoryVersionConflict as exc:
            raise MindMapError(f"读取文档失败：{exc}，请重试") from exc
        if (type(self.engine) is MindMapEngine
                and getattr(self.engine.replace_document, "__func__", None) is _REPLACE_DOCUMENT):
            # 目录解码结果尚未发布，可在预检后直接交给标准引擎。
            if (validation is not None
                    and getattr(self.engine.adopt_document, "__func__", None) is _ADOPT_DOCUMENT):
                self.engine.adopt_document(document, validation=validation)
            else:
                self.engine.adopt_document(document)
        else:
            self.engine.replace_document(document)
        self.accept_directory_version(source, version)
        # 初始视图使用正常倍率；由画布等待实际显示后居中，避免大型导图被整图
        # 适应压缩到无法阅读，也避免在会话安装前使用隐藏视口的占位尺寸。
        self.canvas.reset_view()
        return True

    @staticmethod
    def validate_save_path(path: str | Path) -> Path:
        """规范化导图目录的绝对路径，检查其父目录可用；目标内容在保存时校验。"""
        target = Path(path).resolve()
        if target == target.parent or not target.parent.is_dir():
            raise MindMapError("请选择导图的父目录和目录名")
        return target

    def _choose_save_path(self) -> Path | None:
        """先选择父目录，再输入一个目录名；任一步取消均返回 None。"""
        default = str(self.path.parent) if self.path is not None else ""
        parent = QFileDialog.getExistingDirectory(self.dialog_parent, "选择导图的父目录", default)
        if not parent:
            return None
        name, accepted = QInputDialog.getText(
            self.dialog_parent, "保存导图", "目录名", text=self.path.name if self.path else "我的思维导图",
        )
        if not accepted:
            return None
        name = name.strip()
        if not name or name in {".", ".."} or any(char in name for char in '/\\:\0'):
            raise MindMapError("请输入单个有效的目录名")
        return Path(parent) / name

    def save(self, *, save_as=False):
        """提交有效输入并保存当前导图；首次保存和另存为需要选择目标目录。"""
        if self.prepare_save is not None and not self.prepare_save():
            return False
        self.panels.flush_pending_edits()
        self.canvas.finish_edit(commit=True)
        target = self.path
        choosing = save_as or target is None
        if choosing:
            try:
                target = self._choose_save_path()
            except MindMapError as exc:
                self.report_error(exc)
                return False
            if target is None:
                return False
        return self._write_snapshot(target, confirm_overwrite=choosing)

    def save_committed(self) -> bool:
        """只保存已提交快照，不弹出选择框、不结束原地编辑或提交侧栏草稿。

        自动保存调用此边界；保存通知仅更新历史的保存位置，不产生新的编辑记录。
        无绑定路径时报告错误并返回 False。即使快照没有修改，也检查路径、原生
        磁盘版本，避免目录丢失或发生外部修改时被当作保存成功。
        """
        if self.path is None:
            self.report_error("导图尚未选择保存位置")
            return False
        return self._write_snapshot(self.path, only_if_dirty=True)

    def restore_missing(self) -> bool:
        """将已提交内容恢复到原路径。写入前再次检查目录尚未重现。"""
        return self.path is not None and self._write_snapshot(self.path, restore_missing=True)

    def expected_write_version(self, target: Path) -> DirectoryVersion | None:
        """返回待比较的磁盘版本；原路径使用上次打开或保存的版本，另存取当前版本。"""
        if self.state.version is not None and self._is_bound_target(target):
            return self.state.version
        version = directory_version(target)
        if version is not None and not version.has_manifest:
            raise MindMapError("目标目录已存在且缺少导图清单")
        return version

    def _is_bound_target(self, target: Path) -> bool:
        return self.path is not None and (target == self.path or target.resolve() == self.path.resolve())

    def accept_directory_version(self, target: Path, version: DirectoryVersion) -> None:
        """记录已打开或写入的目录版本，解除冲突并通知所有关联视图。"""
        self.state.path, self.state.version, self.state.conflict = target, version, False
        self.state_changed.emit()

    def _write_snapshot(self, target, *, only_if_dirty=False, restore_missing=False, confirm_overwrite=False):
        """统一手动及自动保存；检查目录版本后写入，成功才更新绑定路径。"""
        try:
            target = self.validate_save_path(target)
            if self.wait_for_pending_write is not None:
                self.wait_for_pending_write()
            if self.check_save_target is not None:
                self.check_save_target(target)
            expected = None if restore_missing else self.expected_write_version(target)
            if confirm_overwrite and expected is not None and not self._is_bound_target(target):
                if QMessageBox.question(
                    self.dialog_parent, "替换导图",
                    f"目录「{target.name}」中已有导图，是否替换其中的导图内容？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                ) != QMessageBox.StandardButton.Yes:
                    return False
            if only_if_dirty and not self.is_dirty:
                check_directory_version(target, expected)
                self.state.conflict = False
                return True
            if self.writer == self._versioned_writer:
                version = self.writer(self.engine.document, target, expected_version=expected)
            else:
                check_directory_version(target, expected)
                version = self.writer(self.engine.document, target)
            if not isinstance(version, DirectoryVersion):
                version = directory_version(target)
            if version is None:
                raise MindMapError("写入后未找到导图目录")
        except DirectoryVersionConflict as exc:
            # 另存目标被修改时保留原绑定文件的冲突状态，允许稍后继续保存原文件。
            self.report_error(exc if self._is_bound_target(target) else str(exc))
            return False
        except (MindMapError, OSError) as exc:
            self.report_error(exc)
            return False
        self.state.path = target
        self.engine.mark_saved()
        self.accept_directory_version(target, version)
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

