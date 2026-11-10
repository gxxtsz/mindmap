"""标准编辑命令按活动编辑器与局部输入焦点路由，避免分屏中的重复快捷键。"""

from PySide6.QtCore import QObject
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication, QLineEdit, QPlainTextEdit, QTextEdit
from shiboken6 import isValid


class EditCommandRouter(QObject):
    """宿主只认识标准动作名，业务能力由 EditorView.edit_actions 提供。

    输入框原生的 ShortcutOverride 保留 Qt 编辑体验；通过菜单触发时也优先操作
    仍然获得焦点的输入框。导图画布等非文本控件使用当前视图提供的 QAction。
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.actions = {}
        for key, title, shortcuts in (
            ("undo", "撤销", ["Ctrl+Z"]), ("redo", "重做", ["Ctrl+Y", "Ctrl+Shift+Z"]),
            ("cut", "剪切", ["Ctrl+X"]), ("copy", "复制", ["Ctrl+C"]),
            ("paste", "粘贴", ["Ctrl+V"]), ("select_all", "全选", ["Ctrl+A"]),
        ):
            action = window.manager.register_action("app." + key, title,
                                                     lambda checked=False, command=key: self.execute(command))
            action.setShortcuts([QKeySequence(value) for value in shortcuts])
            self.actions[key] = action
        QApplication.instance().focusChanged.connect(self.refresh)

    def local_input(self):
        widget = QApplication.focusWidget()
        if isinstance(widget, (QLineEdit, QPlainTextEdit, QTextEdit)) and self.window.isAncestorOf(widget):
            return widget
        return None

    def execute(self, key):
        """每次触发都重新读取焦点和活动视图，禁止保存旧标签回调。"""
        local = self.local_input()
        if local is not None:
            getattr(local, "selectAll" if key == "select_all" else key)()
        else:
            source = getattr(self.window.documents.active, "edit_actions", {}).get(key)
            if source is not None and isValid(source) and source.isEnabled():
                self.window.call(source.trigger)
        self.refresh()

    def refresh(self, *_):
        if self.window._disposed:
            return
        local = self.local_input()
        sources = getattr(self.window.documents.active, "edit_actions", {})
        for key, action in self.actions.items():
            if local is not None:
                enabled = not local.isReadOnly() or key in ("copy", "select_all")
            else:
                source = sources.get(key)
                enabled = source is not None and isValid(source) and source.isEnabled()
            action.setEnabled(enabled)

    def dispose(self):
        QApplication.instance().focusChanged.disconnect(self.refresh)
