"""独立业务项目模板：只通过公开接口使用工作台，业务代码保留在自己的包中。"""

from mindmap_docking import DockPosition, DocumentSpec, Theme, ToolWindowSpec
from mindmap_shell import ActionSpec, AppSpec, WorkbenchWindow, run_desktop
from PySide6.QtWidgets import QLabel, QMessageBox, QPlainTextEdit


def build_window(*, storage_dir=None, theme="light", restore=True):
    """替换应用标识、内容工厂和动作即可构建自己的桌面软件。"""
    window = None
    editor = QPlainTextEdit("欢迎使用桌面工作台。\n\n在这里编写你的业务内容。")
    editor.document().setModified(False)
    log = QPlainTextEdit("应用已启动。")
    log.setReadOnly(True)

    def save():
        # 这里演示“业务已处理修改”的生命周期；真实应用应先完成自己的文件写入。
        editor.document().setModified(False)
        window.manager.set_document_modified("welcome", False)
        log.appendPlainText("示例已标记为已处理；真实应用请接入自己的保存逻辑。")

    def can_close():
        if not editor.document().isModified():
            return True
        choice = QMessageBox.question(window, "内容尚未处理", "放弃当前示例修改并关闭？",
                                      QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                                      QMessageBox.StandardButton.Cancel)
        return choice == QMessageBox.StandardButton.Discard

    spec = AppSpec("my-desktop-app", "我的桌面应用", subtitle="业务工作空间", storage_dir=storage_dir,
                   theme=Theme(theme), actions=(
                       ActionSpec("app.save", "标记已处理", save, "save", "Ctrl+S", "文件", True),
                   ))
    window = WorkbenchWindow(spec)
    manager = window.manager
    manager.register_document(DocumentSpec("welcome", "欢迎", lambda: editor, can_close=can_close))
    editor.textChanged.connect(lambda: manager.set_document_modified("welcome", editor.document().isModified()))
    manager.register_tool_window(ToolWindowSpec("navigation", "导航", lambda: QLabel("在这里放置业务导航树。"),
                                               "folder", DockPosition.LEFT_TOP, "Alt+1"))
    manager.register_tool_window(ToolWindowSpec("logs", "日志", lambda: log, "log", DockPosition.BOTTOM_LEFT, "Alt+2"))
    manager.activate("navigation", focus=False)
    manager.activate("logs", focus=False)
    manager.set_default_layout()
    if restore:
        manager.load_session()
    return window


def main(argv=None):
    return run_desktop(lambda args: build_window(storage_dir=args.state_dir, theme=args.theme or "light",
                                               restore=not args.no_restore), argv, description="我的桌面应用")


if __name__ == "__main__":
    raise SystemExit(main())
