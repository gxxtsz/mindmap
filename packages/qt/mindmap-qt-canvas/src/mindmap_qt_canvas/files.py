"""宿主无关的文件生命周期：原生导图打开、保存及独立图形导出。"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from mindmap_core import FormatRegistration, MindMapDocument, MindMapError
from mindmap_formats import load_document, save_document
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QFileDialog, QMessageBox


class PendingEdits(Protocol):
    """文件操作前提交面板输入的最小协议，不要求安装面板或工作区包。

    独立画布宿主可以传入提供同名方法的对象；没有侧栏时该方法可以为空实现。
    有输入法候选或无效字段时，遵循宿主原有处理，禁止强制提交尚未完成的文字。
    """

    def flush_pending_edits(self) -> None:
        """同步提交已完成的有效输入；返回后文件控制器再结束画布文字编辑。"""
        ...


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

    def __init__(self, canvas, panels: PendingEdits, dialog_parent=None, *, writer=save_document):
        super().__init__(dialog_parent)
        self.canvas, self.engine, self.panels = canvas, canvas.engine, panels
        self.dialog_parent = dialog_parent
        self.writer = writer
        self.path: Path | None = None
        # 宿主如有后台写入，可注入 GUI 线程等待边界；路径切换和显式保存先收束旧写入。
        self.wait_for_pending_write = None

    @property
    def is_dirty(self) -> bool:
        """只读返回引擎修改状态；原生导图打开不再产生待另存的导入状态。"""
        return self.engine.is_dirty

    def report_error(self, message):
        """把失败交给宿主的状态栏或日志，不自行显示阻塞错误对话框。"""
        self.error_occurred.emit(message)

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
        source = self.validate_open_path(path)
        registry = self.canvas.format_registry
        document = load_document(source, registry=registry, format_id="mindmap")
        self.canvas.validate_document(document)
        if not self.confirm_replace():
            return False
        if self.wait_for_pending_write is not None:
            self.wait_for_pending_write()
        self.engine.replace_document(document)
        self.path = source
        self.state_changed.emit()
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

    def save_committed(self):
        """只保存已提交快照，不弹出选择框、不结束原地编辑或提交侧栏草稿。

        自动保存调用此边界；保存通知仅更新历史的保存位置，不产生新的编辑记录。
        无绑定路径时报告错误并返回 False。即使当前快照没有修改，也先校验路径
        和原生编解码器，避免错误绑定路径在保存全部或关闭时被当作保存成功。
        """
        if self.path is None:
            self.report_error("导图尚未选择保存位置")
            return False
        return self._write_snapshot(self.path, only_if_dirty=True)

    def _write_snapshot(self, target, *, only_if_dirty=False):
        """手动、自动及首次保存共用原生路径检查和原子写入，失败不更新绑定路径。

        自动保存的干净快照可跳过写盘，但不能跳过路径与格式检查。写入器始终接收
        明确的 mindmap 标识和补齐后的路径，因此扩展格式及路径占用检查无法绕过。
        """
        try:
            target = self.validate_save_path(target)
            self.require_native_save_format()
            if self.wait_for_pending_write is not None:
                self.wait_for_pending_write()
            if only_if_dirty and not self.is_dirty:
                return True
            self.writer(
                self.engine.document, target, registry=self.canvas.format_registry, format_id="mindmap"
            )
        except (MindMapError, OSError) as exc:
            self.report_error(str(exc))
            return False
        self.path = target
        self.engine.mark_saved()
        self.state_changed.emit()
        return True

    def export_dialog(self, format_id: str = "svg"):
        """为 png、svg 或 pdf 打开单格式保存对话框，默认使用 svg。

        宿主菜单先选择格式，对话框只显示该格式并给出相应默认文件名；取消时
        不询问折叠范围。输入提交和原有折叠范围选择继续保留，不改变保存位置。
        无扩展名时补齐所选格式；不同扩展名直接报错，避免输出类型偏离菜单选择。
        """
        formats = {"png": "PNG 图片", "svg": "SVG 矢量图", "pdf": "PDF 文档"}
        if not isinstance(format_id, str) or format_id not in formats:
            raise MindMapError("支持的图形导出格式为 PNG、SVG、PDF")
        label, extension = formats[format_id], "." + format_id
        self.panels.flush_pending_edits()
        self.canvas.finish_edit(commit=True)
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
        expand = QMessageBox.question(
            self.dialog_parent,
            "导出范围",
            "是否展开全部节点后导出？\n选择“否”将保留当前折叠状态。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        self.canvas.export(target, expand_all=expand == QMessageBox.StandardButton.Yes)
        self.message.emit(f"已导出：{target}")

