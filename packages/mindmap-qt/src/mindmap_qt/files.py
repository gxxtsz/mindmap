"""宿主无关的文件生命周期：打开与保存格式筛选、保存决策及图形导出。"""

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

    def __init__(self, canvas, panels, dialog_parent=None, *, writer=save_document):
        super().__init__(dialog_parent)
        self.canvas, self.engine, self.panels = canvas, canvas.engine, panels
        self.dialog_parent = dialog_parent
        self.writer = writer
        self.path: Path | None = None
        self._path_format_id: str | None = None
        self._unsaved_import = False

    @property
    def is_dirty(self) -> bool:
        """有损导入即使尚无编辑历史，也必须另存为可完整往返的文档。"""
        return self.engine.is_dirty or self._unsaved_import

    def report_error(self, message):
        """把失败交给宿主的状态栏或日志，不自行显示阻塞错误对话框。"""
        self.error_occurred.emit(message)

    def _update_status(self):
        self.state_changed.emit()

    def format_filters(self, purpose: str) -> str:
        """按 open 或 save 生成当前格式筛选项，其他用途抛出 MindMapError。

        打开允许所有具备读取能力的格式，主文档保存只允许完整往返的格式；
        通用写入器仍由保存和剪贴板复用，不再提供文档导出专用筛选。
        """
        return ";;".join(self._format_label(entry) for entry in self._format_entries(purpose))

    def _format_entries(self, purpose):
        entries = self.canvas.format_registry.list()
        if purpose == "open":
            return tuple(entry for entry in entries if entry.importer is not None)
        if purpose == "save":
            return tuple(entry for entry in entries if entry.can_save)
        raise MindMapError("未知文件对话框用途")

    @staticmethod
    def _format_label(entry):
        return f"{entry.info.name} ({' '.join('*' + ext for ext in entry.info.extensions)})"

    def _selected_format(self, selected, purpose):
        matches = [
            entry.info.id for entry in self._format_entries(purpose) if self._format_label(entry) == selected
        ]
        if len(matches) > 1:
            raise MindMapError("格式筛选项存在歧义，请为扩展使用不同名称")
        return matches[0] if matches else None

    def confirm_replace(self) -> bool:
        """提交已完成输入并询问保存；取消或保存失败返回 False，不释放任何控件。"""
        self.panels.flush_pending_edits()
        self.canvas.finish_edit(commit=True)
        if not self.engine.is_dirty and not self._unsaved_import:
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
            self._unsaved_import = False
            self.engine.replace_document(MindMapDocument.new())
            self.canvas.center_root()
            self.state_changed.emit()
            return True
        return False

    def open_dialog(self):
        """按当前导入能力选择文件，返回是否成功打开；不缓存格式列表。"""
        path, selected = QFileDialog.getOpenFileName(
            self.dialog_parent, "打开 / 导入", "", self.format_filters("open") + ";;所有文件 (*)"
        )
        if path:
            return self.open_path(path, format_id=self._selected_format(selected, "open"))
        return False

    def open_path(self, path: str | Path, *, format_id: str | None = None):
        """解析格式并试算布局成功后再询问替换；有损导入不沿用源文件保存路径。"""
        source = Path(path)
        registry = self.canvas.format_registry
        entry = registry.resolve(source, format_id=format_id)
        document = load_document(source, registry=registry, format_id=entry.info.id)
        self.canvas.validate_document(document)
        if not self.confirm_replace():
            return False
        self.engine.replace_document(document)
        self.path = source if entry.can_save else None
        self._path_format_id = entry.info.id if entry.can_save else None
        self._unsaved_import = not entry.can_save
        self._update_status()
        self.canvas.fit_to_view()
        return True

    def save(self, *, save_as=False):
        """保存成功后才更新文件路径与已保存标记；失败保持脏状态。"""
        self.panels.flush_pending_edits()
        self.canvas.finish_edit(commit=True)
        if not self._format_entries("save"):
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
            format_id = self._selected_format(selected, "save")
            if not target.suffix:
                entry = (
                    self.canvas.format_registry.get(format_id)
                    if format_id is not None
                    else self._format_entries("save")[0]
                )
                format_id = entry.info.id
                target = target.with_suffix(entry.info.extensions[0])
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
        self.path, self._unsaved_import = target, False
        self._path_format_id = entry.info.id
        self.engine.mark_saved()
        self.state_changed.emit()
        return True

    def export_dialog(self):
        """提交有效输入后选择图形格式与折叠范围，导出不改变保存标记。"""
        self.panels.flush_pending_edits()
        self.canvas.finish_edit(commit=True)
        path, selected = QFileDialog.getSaveFileName(
            self.dialog_parent, "导出图形", "思维导图.svg", "SVG 矢量图 (*.svg);;PNG 图片 (*.png);;PDF 文档 (*.pdf)"
        )
        if not path:
            return
        target = Path(path)
        if not target.suffix:
            target = target.with_suffix(
                ".png" if "PNG" in selected else ".pdf" if "PDF" in selected else ".svg"
            )
        expand = QMessageBox.question(
            self.dialog_parent,
            "导出范围",
            "是否展开全部节点后导出？\n选择“否”将保留当前折叠状态。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        self.canvas.export(target, expand_all=expand == QMessageBox.StandardButton.Yes)
        self.message.emit(f"已导出：{target}")

