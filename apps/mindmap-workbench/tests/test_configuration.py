"""按真实能力归属迁移的组件与行为回归。"""










from mindmap_docking import DockManager
from mindmap_workbench.shell_adapter import build_window
from PySide6.QtCore import QSettings, QStandardPaths


def test_project_configuration_namespace_and_preferences_are_isolated(qtbot, tmp_path, monkeypatch):
    """默认配置位于项目命名空间，应用读取自己的布局位置与滚轮偏好。"""
    monkeypatch.setattr(QStandardPaths, "writableLocation", lambda location: str(tmp_path))
    configuration = tmp_path / "mindmap/mindmap-workbench"
    configuration.mkdir(parents=True)
    preferences = QSettings(str(configuration / "preferences.ini"), QSettings.Format.IniFormat)
    preferences.setValue("view/wheel_mode", "zoom")
    preferences.sync()
    manager = DockManager(application_id="mindmap-workbench")
    assert manager.storage_dir == configuration and manager.storage_file == configuration / "layouts-v1.json"
    manager.shutdown()
    window = build_window(restore=False)
    qtbot.addWidget(window)
    assert window.canvas is None
    assert window.new_document(tmp_path / "配置隔离.mindmap")
    assert window.manager.storage_dir == configuration and window.canvas.wheel_mode == "zoom"
    window.close()
