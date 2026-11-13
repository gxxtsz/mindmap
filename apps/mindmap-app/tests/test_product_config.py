"""新产品使用导图标识和配置目录，历史位置由迁移层单独读取。"""

from mindmap_app.app import build_window, default_storage_dir
from PySide6.QtCore import QStandardPaths


def test_product_factory_uses_mindmap_storage_and_layout_identity(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(QStandardPaths, "writableLocation", lambda location: str(tmp_path))
    expected = tmp_path / "mindmap"
    assert default_storage_dir() == expected
    window = build_window(restore=False)
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    assert window.manager.application_id == "mindmap"
    assert window.manager.storage_file.parent == expected
    assert window.windowTitle() == "思维导图"
    assert window.close()
    assert (expected / "mindmap-state-v2.json").is_file()
    restored = build_window()
    qtbot.addWidget(restored, before_close_func=lambda item: item.dispose())
    assert restored.manager.storage_file.parent == expected
    assert not restored.state.error
