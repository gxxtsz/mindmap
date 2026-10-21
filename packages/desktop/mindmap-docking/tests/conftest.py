"""使用真实 Qt 控件验证停靠行为；测试数据与用户配置完全隔离。"""

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from mindmap_docking import DockHost, DockManager, DockPosition, ToolWindowSpec, ViewMode
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QMainWindow, QPlainTextEdit


@dataclass
class DockingSystem:
    """测试宿主及业务控件工厂计数，便于从公开状态核验实例保留。"""

    manager: DockManager
    host: DockHost
    window: QMainWindow
    document: QPlainTextEdit
    calls: dict[str, int] = field(default_factory=dict)

    def register(self, tool_id, position=DockPosition.LEFT_TOP, mode=ViewMode.DOCK_PINNED,
                 shortcut="", factory=None):
        """注册最小业务面板；工厂调用次数独立于组件内部实现记录。"""
        def create():
            self.calls[tool_id] = self.calls.get(tool_id, 0) + 1
            return factory() if factory else QPlainTextEdit("初始内容：" + tool_id)

        return self.manager.register_tool_window(
            ToolWindowSpec(tool_id, "测试窗口 " + tool_id, create,
                           position=position, mode=mode, shortcut=shortcut)
        )


@pytest.fixture
def system_factory(qapp, qtbot, tmp_path):
    """允许同一用例创建两个独立会话，验证跨会话 MIME 与磁盘恢复边界。

    退出必须先调用管理器 shutdown，断开 QApplication 全局事件与延迟保存；
    再关闭主窗口和释放延迟删除对象，避免下一用例收到旧管理器的焦点回调。
    """
    systems = []

    def create(storage_dir: Path | None = None):
        directory = storage_dir or tmp_path / ("会话-" + str(len(systems)))
        manager = DockManager(storage_dir=directory)
        window = QMainWindow()
        window.setWindowTitle("停靠回归测试")
        window.resize(1120, 760)
        host = DockHost(manager, "main")
        window.setCentralWidget(host)
        document = QPlainTextEdit("def sample():\n    return '编辑器内容保留'\n")
        manager.add_document("doc", "sample.py", document)
        system = DockingSystem(manager, host, window, document)
        system.register("alpha", DockPosition.LEFT_TOP, shortcut="Alt+1")
        system.register("beta", DockPosition.LEFT_BOTTOM, shortcut="Alt+7")
        system.register("gamma", DockPosition.BOTTOM_LEFT, shortcut="Alt+F12")
        system.register("peer", DockPosition.LEFT_TOP)
        manager.refresh()
        window.show()
        window.activateWindow()
        document.setFocus()
        qtbot.waitUntil(window.isVisible)
        QApplication.processEvents()
        manager.set_default_layout()
        systems.append(system)
        return system

    yield create

    popup = QApplication.activePopupWidget()
    if popup is not None:
        popup.close()
    for system in reversed(systems):
        system.manager.shutdown()
        system.window.close()
        system.window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


@pytest.fixture
def docking(system_factory):
    """默认注册四个工具窗但不激活，便于验证延迟工厂与默认布局。"""
    return system_factory()
