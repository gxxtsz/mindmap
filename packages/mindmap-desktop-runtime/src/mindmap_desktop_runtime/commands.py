"""标准编辑命令按活动编辑器与局部输入焦点路由，避免分屏中的重复快捷键。"""

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication, QLineEdit, QPlainTextEdit, QTextEdit
from shiboken6 import isValid


class EditCommandRouter(QObject):
    """宿主只认识标准动作名，业务能力由 EditorView.edit_actions 提供。

    输入框原生的 ShortcutOverride 保留 Qt 编辑体验。键盘进入菜单时记录原输入框，
    菜单中的命令仍可操作该输入框。导图画布等非文本控件使用当前视图提供的 QAction。
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.actions = {}
        self._menu_input = None
        self._menu_line_selection: tuple[QLineEdit, str, int, int] | None = None
        for key, title, shortcuts in (
            ("undo", "撤销", ["Ctrl+Z"]), ("redo", "重做", ["Ctrl+Y", "Ctrl+Shift+Z"]),
            ("cut", "剪切", ["Ctrl+X"]), ("copy", "复制", ["Ctrl+C"]),
            ("paste", "粘贴", ["Ctrl+V"]), ("select_all", "全选", ["Ctrl+A"]),
        ):
            action = window.manager.register_action("app." + key, title,
                                                     lambda checked=False, command=key: self.execute(command))
            action.setShortcuts([QKeySequence(value) for value in shortcuts])
            self.actions[key] = action
        QApplication.instance().focusChanged.connect(self._focus_changed)

    def _is_menu_widget(self, widget) -> bool:
        menu_bar = self.window.title_bar.menu_bar
        return widget is not None and isValid(widget) and (widget is menu_bar or menu_bar.isAncestorOf(widget))

    def _is_local_input(self, widget) -> bool:
        return (isinstance(widget, (QLineEdit, QPlainTextEdit, QTextEdit))
                and isValid(widget) and self.window.isAncestorOf(widget))

    def _focus_changed(self, previous, current) -> None:
        """记录进入本窗口菜单时的文本焦点，离开菜单后恢复即时焦点路由。"""
        selection, self._menu_line_selection = self._menu_line_selection, None
        if isinstance(previous, QLineEdit) and isValid(previous):
            previous.removeEventFilter(self)
        if isinstance(current, QLineEdit) and self._is_local_input(current):
            current.installEventFilter(self)
        if self._is_menu_widget(current):
            if self._is_local_input(previous):
                self._menu_input = previous
                if selection is not None:
                    field, text, anchor, length = selection
                    if (field is previous and field.isVisible() and field.text() == text
                            and not field.hasSelectedText()):
                        field.setSelection(anchor, length)
        else:
            self._menu_input = None
        self.refresh()

    def eventFilter(self, widget, event) -> bool:
        """在单行输入框处理菜单焦点事件前保存选区，保留光标方向及 Qt 字符位置。"""
        if (event.type() == QEvent.Type.FocusOut and event.reason() == Qt.FocusReason.MenuBarFocusReason
                and widget.hasSelectedText()):
            cursor = widget.cursorPosition()
            anchor = widget.selectionEnd() if cursor == widget.selectionStart() else widget.selectionStart()
            self._menu_line_selection = (widget, widget.text(), anchor, cursor - anchor)
        return False

    def local_input(self):
        """返回当前文本控件，菜单临时持有焦点时使用仍然可见的原输入框。"""
        widget = QApplication.focusWidget()
        if self._is_local_input(widget):
            return widget
        if self._is_menu_widget(widget):
            target = self._menu_input
            return target if self._is_local_input(target) and target.isVisible() else None
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
        """撤销焦点跟踪及当前单行输入框的事件观察，释放菜单期间记录的引用。"""
        QApplication.instance().focusChanged.disconnect(self._focus_changed)
        widget = QApplication.focusWidget()
        if isinstance(widget, QLineEdit) and self._is_local_input(widget):
            widget.removeEventFilter(self)
        self._menu_input = None
        self._menu_line_selection = None
