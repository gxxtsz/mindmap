"""停靠组件默认的最小原生框架；完整应用外壳由可选 mindmap_shell 提供。"""

from PySide6.QtCore import Slot
from PySide6.QtWidgets import QMainWindow

from .host import DockHost


class DockFrame(QMainWindow):
    """不依赖工作台外壳的辅助窗口；关闭时先把内容交还主宿主。"""

    def __init__(self, manager, host_id="main"):
        super().__init__()
        self.manager = manager
        self.host_id = host_id
        self._retiring = False
        self.setWindowTitle("工作区" + ("" if host_id == "main" else " · " + host_id))
        self.resize(1100, 760)
        self.host = DockHost(manager, host_id, self)
        self.setCentralWidget(self.host)
        manager.theme_changed.connect(self.refresh_theme)
        manager.message.connect(self.show_status_message)
        self.refresh_theme()

    @Slot()
    def refresh_theme(self):
        self.manager.theme.apply(self)

    @Slot(str)
    def show_status_message(self, text):
        self.statusBar().showMessage(text, 6000)

    def closeEvent(self, event):
        if self._retiring or self.manager.closing:
            event.accept()
        elif self.host_id == "main":
            if self.manager.can_close_documents():
                self.manager.shutdown()
                event.accept()
            else:
                event.ignore()
        else:
            self.manager.close_frame(self.host_id)
            event.accept()
