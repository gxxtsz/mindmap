"""布局写入屏障：嵌套事件循环期间保护旧会话，完成后恢复防抖保存。"""

import json

import pytest
from PySide6.QtCore import QTimer


def test_nested_deferral_blocks_explicit_and_queued_saves_until_complete(docking, qtbot):
    manager = docking.manager
    manager.save_session()
    original = manager.storage_file.read_bytes()
    with manager.defer_session_saving():
        manager.set_theme("dark")
        manager.save_session()
        with manager.defer_session_saving():
            manager.set_option("compact", True)
            QTimer.singleShot(0, manager.save_session)
            qtbot.wait(300)
            assert manager.storage_file.read_bytes() == original
        # 内层退出不能提前解除外层对自动保存的保护。
        qtbot.wait(300)
        assert manager.storage_file.read_bytes() == original
    qtbot.waitUntil(lambda: manager.storage_file.read_bytes() != original)
    restored = json.loads(manager.storage_file.read_bytes())["session"]
    assert restored["theme"] == "dark" and restored["settings"]["compact"]


def test_failed_deferral_does_not_automatically_write_partial_state(docking, qtbot):
    manager = docking.manager
    manager.save_session()
    original = manager.storage_file.read_bytes()
    with pytest.raises(RuntimeError, match="恢复未完成"):
        with manager.defer_session_saving():
            manager.set_theme("dark")
            raise RuntimeError("恢复未完成")
    qtbot.wait(300)
    assert manager.storage_file.read_bytes() == original
    # 退出失败作用域后，后续正常操作仍可显式保存，不会永久禁用持久化。
    manager.save_session()
    assert json.loads(manager.storage_file.read_bytes())["session"]["theme"] == "dark"


def test_shutdown_during_deferral_keeps_last_complete_session(docking, qtbot):
    manager = docking.manager
    manager.save_session()
    original = manager.storage_file.read_bytes()
    with manager.defer_session_saving():
        manager.set_theme("dark")
        manager.shutdown()
    qtbot.wait(300)
    assert manager.storage_file.read_bytes() == original
