"""产品改名后的存储兼容性：沿用历史应用标识、目录及布局归属。"""

from personal_workbench.app import build_window, default_storage_dir
from PySide6.QtCore import QStandardPaths


def test_product_factory_preserves_historical_storage_and_layout_identity(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(QStandardPaths, "writableLocation", lambda location: str(tmp_path))
    expected = tmp_path / "mindmap" / "mindmap-workbench"
    assert default_storage_dir() == expected
    window = build_window(manifests=[], restore=False)
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    assert window.manager.application_id == "mindmap-workbench"
    assert window.manager.storage_file.parent == expected
    assert window.windowTitle() == "个人工作台"
    assert window.close()
    assert (expected / "workbench-state-v1.json").is_file()
    restored = build_window(manifests=[])
    qtbot.addWidget(restored, before_close_func=lambda item: item.dispose())
    assert restored.manager.storage_file.parent == expected
    assert not restored.state.error
