"""UTF-8 纯文本编辑器：共享内容与撤销历史，独立光标及滚动状态。"""

import os
import tempfile
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QAction, QFontDatabase, QTextCursor, QTextDocument
from PySide6.QtWidgets import QFileDialog, QPlainTextDocumentLayout, QPlainTextEdit
from workbench_api import EditorProvider, OperationResult, SavePolicy


class TextDocument(QObject):
    """文档拥有 QTextDocument；任何一个标签关闭都不能连带销毁其他视图的内容。"""

    save_policy = SavePolicy.MANUAL

    def __init__(self, plugin, path=None):
        super().__init__(plugin)
        self.plugin = plugin
        self.id = "document." + uuid4().hex
        self.path = Path(path).resolve() if path else None
        self.suggested_path = None
        self.bom = False
        self.newline = "\n"
        self.last_error = ""
        self.views = []
        self.content = QTextDocument(self)
        self.content.setDocumentLayout(QPlainTextDocumentLayout(self.content))
        if self.path:
            raw = self.path.read_bytes()
            self.bom = raw.startswith(b"\xef\xbb\xbf")
            text = raw.decode("utf-8-sig", errors="strict")
            if "\0" in text:
                raise ValueError("文件包含二进制内容，无法作为纯文本打开")
            counts = {"\r\n": text.count("\r\n"), "\n": text.count("\n") - text.count("\r\n"),
                      "\r": text.count("\r") - text.count("\r\n")}
            if any(counts.values()):
                self.newline = max(counts, key=counts.get)
            self.content.setPlainText(text.replace("\r\n", "\n").replace("\r", "\n"))
        self.content.setModified(False)
        self.content.contentsChanged.connect(self.changed)
        self.content.modificationChanged.connect(self.changed)

    @property
    def is_dirty(self):
        return self.content.isModified()

    @property
    def resource_uri(self):
        return self.path.as_uri() if self.path else "untitled:" + self.id

    def changed(self, *_):
        for view in tuple(self.views):
            view.sync_actions()
            self.plugin.context.documents.sync(view)

    def save(self, *, save_as=False, reason="explicit"):
        """先完成输入与路径检查，再原子替换文件；取消和失败均保留原路径及修改状态。"""
        if any(not view.prepare_input() for view in self.views):
            return OperationResult.cancelled("请先完成输入法候选，再保存文本")
        target = self.path
        if target is None or save_as:
            selected, _ = QFileDialog.getSaveFileName(
                self.plugin.context.ui.parent, "保存文本", str(target or self.suggested_path or "未命名.txt"),
                "文本文件 (*.txt);;Markdown 源文件 (*.md)")
            if not selected:
                return OperationResult.cancelled()
            target = Path(selected).resolve()
            if not target.suffix:
                target = target.with_suffix(".txt")
        if target.suffix.lower() not in (".txt", ".md"):
            return OperationResult.failed("文本编辑器只支持保存为 .txt 或 .md")
        other = self.plugin.context.documents.find_path(target)
        if other is not None and other.document is not self:
            return OperationResult.failed("目标文件已在其他文档中打开")
        temporary = None
        try:
            body = self.content.toPlainText().replace("\n", self.newline)
            raw = body.encode("utf-8-sig" if self.bom else "utf-8")
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".workbench-text-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except (OSError, UnicodeError) as error:
            self.last_error = str(error)
            self.changed()
            self.plugin.context.log(f"保存文本失败：{error}", level="error", source=target.name,
                                    source_path=str(target))
            return OperationResult.failed(error)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        self.path, self.last_error = target, ""
        self.content.setModified(False)
        self.changed()
        if reason == "explicit":
            self.plugin.context.log(f"保存文本：{target}", source=target.name, source_path=str(target))
        return OperationResult()


class TextEdit(QPlainTextEdit):
    """仅记录输入法组合状态；原生编辑、选择和快捷键仍交给 Qt。"""

    def __init__(self):
        super().__init__()
        self.composing = False
        self.restore_scroll_pending = None

    def inputMethodEvent(self, event):
        super().inputMethodEvent(event)
        self.composing = bool(event.preeditString())

    def showEvent(self, event):
        """隐藏标签在首次实际显示后恢复滚动，避免按占位控件尺寸提前裁掉位置。"""
        super().showEvent(event)
        if self.restore_scroll_pending is not None:
            QTimer.singleShot(0, self, self.restore_scroll_pending)


class TextView:
    icon = "document"

    def __init__(self, plugin, document, view_id=None, state=None):
        self.plugin, self.document = plugin, document
        self.id = view_id or "text." + uuid4().hex
        self.disposed = False
        self.widget = TextEdit()
        self.widget.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.widget.setDocument(document.content)
        self.actions = {}
        for key, title, shortcut in (("undo", "撤销", "Ctrl+Z"), ("redo", "重做", "Ctrl+Shift+Z"),
                                     ("cut", "剪切", "Ctrl+X"), ("copy", "复制", "Ctrl+C"),
                                     ("paste", "粘贴", "Ctrl+V"), ("selectAll", "全选", "Ctrl+A")):
            action = QAction(title, self.widget)
            # 快捷键由平台统一注册；QPlainTextEdit 的原生输入快捷键仍优先处理。
            action.triggered.connect(getattr(self.widget, key))
            self.widget.addAction(action)
            self.actions[key] = action
        self.toolbar_actions = (self.actions["undo"], self.actions["redo"])
        self.edit_actions = {"select_all" if key == "selectAll" else key: action
                             for key, action in self.actions.items()}
        self.menu_actions = {"编辑": tuple(self.actions.values())}
        document.views.append(self)
        self.widget.selectionChanged.connect(self.sync_actions)
        document.content.undoAvailable.connect(self.sync_actions)
        document.content.redoAvailable.connect(self.sync_actions)
        if state:
            self.restore_state(state)
        self.sync_actions()

    @property
    def title(self):
        name = self.document.path.stem if self.document.path else "未命名"
        return name + ("（保存失败）" if self.document.last_error else "")

    @property
    def status_text(self):
        return f"{self.document.content.characterCount() - 1} 个字符 · " + (
            "保存失败" if self.document.last_error else "未保存" if self.document.path is None else
            "待保存" if self.document.is_dirty else "已保存")

    def prepare_input(self):
        if self.widget.composing:
            self.plugin.context.ui.show_message("请先完成输入法候选，再保存或关闭文本")
            return False
        return True

    def sync_actions(self, *_):
        self.actions["undo"].setEnabled(self.document.content.isUndoAvailable())
        self.actions["redo"].setEnabled(self.document.content.isRedoAvailable())
        for key in ("copy", "cut"):
            self.actions[key].setEnabled(self.widget.textCursor().hasSelection())

    def capture_state(self):
        cursor = self.widget.textCursor()
        return {"version": 1, "cursor": cursor.position(), "anchor": cursor.anchor(),
                "scroll_x": self.widget.horizontalScrollBar().value(),
                "scroll_y": self.widget.verticalScrollBar().value()}

    def restore_state(self, state):
        """只恢复受支持版本；无效位置夹到当前文本范围，滚动在控件布局完成后设置。"""
        if not isinstance(state, dict) or state.get("version", 1) != 1:
            return
        limit = self.document.content.characterCount() - 1
        cursor = self.widget.textCursor()
        for key, mode in (("anchor", QTextCursor.MoveMode.MoveAnchor), ("cursor", QTextCursor.MoveMode.KeepAnchor)):
            value = state.get(key, 0)
            cursor.setPosition(min(limit, max(0, value if type(value) is int else 0)), mode)
        self.widget.setTextCursor(cursor)

        def scroll():
            if not self.widget.isVisible():
                return
            for key, bar in (("scroll_x", self.widget.horizontalScrollBar()),
                             ("scroll_y", self.widget.verticalScrollBar())):
                value = state.get(key, 0)
                bar.setValue(value if type(value) is int else 0)
            self.widget.restore_scroll_pending = None

        self.widget.restore_scroll_pending = scroll
        QTimer.singleShot(0, self.widget, scroll)

    def duplicate(self):
        if not self.prepare_input():
            return OperationResult.cancelled()
        return self.plugin.add_view(self.document, state=self.capture_state(), after=self.id, remember=False)

    def dispose(self):
        if self.disposed:
            return
        self.disposed = True
        self.document.content.undoAvailable.disconnect(self.sync_actions)
        self.document.content.redoAvailable.disconnect(self.sync_actions)
        self.document.views.remove(self)
        self.widget.deleteLater()
        if not self.document.views:
            self.document.deleteLater()


class TextPlugin(QObject):
    """仅依赖 API 的第二种编辑器，实际使用与导图相同的文档登记和菜单入口。"""

    def __init__(self, context):
        super().__init__(context.ui.parent)
        self.context = context
        context.editors.register(EditorProvider("text.editor", "文本", (".txt", ".md"), self.new, self.open))

    def new(self, path=None):
        document = TextDocument(self)
        if path:
            # 路径参数仅作为首次保存的建议，尚未写入的文档始终使用临时资源标识。
            document.suggested_path = Path(path).resolve()
        return self.add_view(document)

    def open(self, path, view_id=None, state=None):
        try:
            existing = self.context.documents.find_path(path)
            document = existing.document if existing and isinstance(existing, TextView) else TextDocument(self, path)
            return self.add_view(document, view_id=view_id, state=state, remember=view_id is None)
        except UnicodeError as error:
            return OperationResult.failed(ValueError(f"{Path(path).name} 不是有效的 UTF-8 文本，无法打开：{error}"))
        except (OSError, ValueError) as error:
            return OperationResult.failed(error)

    def add_view(self, document, *, view_id=None, state=None, after=None, remember=True):
        view = TextView(self, document, view_id, state)
        try:
            self.context.documents.register_view("text.editor", view, after=after, remember=remember)
        except Exception:
            view.dispose()
            raise
        return OperationResult(view_id=view.id)

    def dispose(self):
        self.deleteLater()
