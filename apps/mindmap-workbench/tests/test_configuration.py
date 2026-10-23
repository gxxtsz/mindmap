"""按真实能力归属迁移的组件与行为回归。"""










import pytest
from mindmap_docking import DockManager
from mindmap_workbench.shell_adapter import build_window
from PySide6.QtCore import QSettings, QStandardPaths, Qt
from PySide6.QtWidgets import QApplication


def test_project_configuration_namespace_and_preferences_are_isolated(qtbot, tmp_path, monkeypatch):
    """默认配置位于项目命名空间，应用读取自己的布局位置与滚轮偏好。"""
    monkeypatch.setattr(QStandardPaths, "writableLocation", lambda location: str(tmp_path))
    configuration = tmp_path / "mindmap/mindmap-workbench"
    configuration.mkdir(parents=True)
    preferences = QSettings(str(configuration / "preferences.ini"), QSettings.Format.IniFormat)
    preferences.setValue("view/wheel_mode", "zoom")
    preferences.setValue("window/always_on_top", True)
    preferences.sync()
    manager = DockManager(application_id="mindmap-workbench")
    assert manager.storage_dir == configuration and manager.storage_file == configuration / "layouts-v1.json"
    manager.shutdown()
    window = build_window(restore=False)
    qtbot.addWidget(window)
    assert window.canvas is None
    assert window.new_document(tmp_path / "配置隔离.mindmap")
    assert window.manager.storage_dir == configuration and window.canvas.wheel_mode == "zoom"
    assert window.is_always_on_top()
    window.close()


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_pin_preference_round_trip_and_empty_window(qtbot, tmp_path, restore, enabled):
    """窗口偏好独立于导图恢复；空白页点击后立即落盘，重建窗口恢复相同状态。"""
    preferences = QSettings(str(tmp_path / "preferences.ini"), QSettings.Format.IniFormat)
    preferences.setValue("window/always_on_top", enabled)
    preferences.sync()
    window = build_window(storage_dir=tmp_path, restore=restore)
    qtbot.addWidget(window)
    assert window.isHidden() and window.canvas is None
    assert window.is_always_on_top() == window.title_bar.pin.isChecked() == enabled
    window.show()
    qtbot.mouseClick(window.title_bar.pin, Qt.MouseButton.LeftButton)
    assert window.is_always_on_top() != enabled
    assert preferences.value("window/always_on_top", type=bool) == (not enabled)
    window.close()
    restored = build_window(storage_dir=tmp_path, restore=restore)
    qtbot.addWidget(restored)
    assert restored.isHidden() and restored.canvas is None
    assert restored.is_always_on_top() == restored.title_bar.pin.isChecked() == (not enabled)
    restored.close()


@pytest.mark.parametrize("value, expected", [(None, False), ("true", True), ("false", False),
                                           ("损坏配置", False), (1, False)])
def test_pin_missing_or_invalid_preference_uses_default(qtbot, tmp_path, value, expected):
    """INI 字符串布尔值显式转换，缺失或损坏值不误开启置顶，也不提前显示窗口。"""
    if value is not None:
        preferences = QSettings(str(tmp_path / "preferences.ini"), QSettings.Format.IniFormat)
        preferences.setValue("window/always_on_top", value)
        preferences.sync()
    window = build_window(storage_dir=tmp_path, restore=False)
    qtbot.addWidget(window)
    QApplication.processEvents()
    assert window.isHidden() and window.is_always_on_top() == expected
    window.close()
