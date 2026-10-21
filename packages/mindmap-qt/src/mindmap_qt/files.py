"""宿主无关的文件生命周期：原生导图打开、完整格式保存及图形导出。"""

from __future__ import annotations

from pathlib import Path

from mindmap_engine import MindMapDocument, MindMapError, load_document, save_document
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QFileDialog, QMessageBox


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
    # 文件打开只接收原生容器；与保存、剪贴板的通用编解码注册表分开约束。
    OPEN_FILTER = "思维导图 (*.mindmap)"

    def __init__(self, canvas, panels, dialog_parent=None, *, writer=save_document):
        super().__init__(dialog_parent)
        self.canvas, self.engine, self.panels = canvas, canvas.engine, panels
        self.dialog_parent = dialog_parent
        self.writer = writer
        self.path: Path | None = None
        self._path_format_id: str | None = None

    @property
    def is_dirty(self) -> bool:
        """只读返回引擎修改状态；原生导图打开不再产生待另存的导入状态。"""
        return self.engine.is_dirty

    def report_error(self, message):
        """把失败交给宿主的状态栏或日志，不自行显示阻塞错误对话框。"""
        self.error_occurred.emit(message)

    def format_filters(self, purpose: str) -> str:
        """按 open 或 save 生成当前格式筛选项，其他用途抛出 MindMapError。

        打开固定为 .mindmap，不受额外格式注册影响；保存继续使用完整往返格式。
        通用编解码器仍由保存和剪贴板使用，不作为文件导入入口。
        """
        if purpose == "open":
            return self.OPEN_FILTER
        if purpose == "save":
            return ";;".join(self._format_label(entry) for entry in self._save_formats())
        raise MindMapError("未知文件对话框用途")

    def _save_formats(self):
        """保存格式仍根据完整往返能力筛选，打开限制不改变写入器和剪贴板。"""
        return tuple(entry for entry in self.canvas.format_registry.list() if entry.can_save)

    @staticmethod
    def _format_label(entry):
        return f"{entry.info.name} ({' '.join('*' + ext for ext in entry.info.extensions)})"

    def _selected_save_format(self, selected):
        """仅解析保存对话框的格式选择；原生文件打开不再接受显式格式覆盖。"""
        matches = [
            entry.info.id for entry in self._save_formats() if self._format_label(entry) == selected
        ]
        if len(matches) > 1:
            raise MindMapError("格式筛选项存在歧义，请为扩展使用不同名称")
        return matches[0] if matches else None

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
            self.path = None
            self._path_format_id = None
            self.engine.replace_document(MindMapDocument.new())
            self.canvas.center_root()
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
        self.engine.replace_document(document)
        self.path = source
        self._path_format_id = "mindmap"
        self.state_changed.emit()
        self.canvas.fit_to_view()
        return True

    def save(self, *, save_as=False):
        """保存成功后才更新文件路径与已保存标记；失败保持脏状态。"""
        self.panels.flush_pending_edits()
        self.canvas.finish_edit(commit=True)
        if not self._save_formats():
            self.report_error("未注册可完整往返文档的保存格式")
            return False
        target = self.path
        format_id = self._path_format_id
        if save_as or target is None:
            filename, selected = QFileDialog.getSaveFileName(
                self.dialog_parent,
                "保存导图",
                str(target or "我的思维导图.mindmap"),
                self.format_filters("save"),
            )
            if not filename:
                return False
            target = Path(filename)
            format_id = self._selected_save_format(selected)
            if not target.suffix:
                entry = (
                    self.canvas.format_registry.get(format_id)
                    if format_id is not None
                    else self._save_formats()[0]
                )
                format_id = entry.info.id
                target = target.with_suffix(entry.info.extensions[0])
        return self._write_snapshot(target, format_id)

    def save_committed(self):
        """只保存已提交快照，不弹出选择框、不结束原地编辑或提交侧栏草稿。

        自动保存调用此边界；保存通知仅更新历史的保存位置，不产生新的编辑记录。
        无绑定路径时报告错误并返回 False，成功后才建立保存标记。
        """
        if self.path is None:
            self.report_error("导图尚未选择保存位置")
            return False
        if not self.is_dirty:
            return True
        return self._write_snapshot(self.path, self._path_format_id)

    def _write_snapshot(self, target, format_id):
        """手动和自动保存共用格式检查及原子写入，失败不更新绑定路径。"""
        try:
            entry = self.canvas.format_registry.resolve(target, format_id=format_id, writing=True)
            if not entry.can_save:
                # 仅写格式仍可由底层注册表处理，但不能绕过主文档的完整保存约束。
                raise MindMapError("当前格式不能完整往返文档，请选择可完整保存导图的格式")
            self.writer(
                self.engine.document, target, registry=self.canvas.format_registry, format_id=entry.info.id
            )
        except (MindMapError, OSError) as exc:
            self.report_error(str(exc))
            return False
        self.path = target
        self._path_format_id = entry.info.id
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

