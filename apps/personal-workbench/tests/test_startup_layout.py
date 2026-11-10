"""大文件后台恢复期间，旧布局不得被尚未完成的启动状态覆盖。"""

import json
from threading import Event

import pytest
from mindmap_core import MindMapError
from mindmap_qt_editor.canvas import loading
from personal_workbench.app import build_window
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QPushButton


def test_live_checkpoint_restores_tabs_split_and_latest_view(qtbot, tmp_path):
    """只复制尚未退出时写好的快照，模拟新进程从最近一轮完整工作现场恢复。"""
    directory = tmp_path / "运行配置"
    window = build_window(storage_dir=directory, restore=False)
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    window.show()
    for name in ("左侧", "右侧"):
        assert window.new_document(tmp_path / f"{name}.mindmap")
    left, right = window.documents.ordered()
    window.host.editor.split_token(right.id, Qt.Orientation.Horizontal)
    window.manager.activate_document(left.id)
    path = window.state.store.path

    def read_state():
        return json.loads(path.read_bytes())

    qtbot.waitUntil(lambda: read_state()["active_id"] == left.id)
    # 等第一轮提交完成后只做视口操作，验证无需编辑正文、切换标签或正常退出。
    left.canvas.view.zoom(1.5)
    qtbot.waitUntil(lambda: read_state()["views"][0]["view_state"]["zoom"] == 1.5)
    saved = read_state()
    assert saved["session_checkpoint"] and saved["layout"]["session"]["hosts"]["main"]["editor"]["kind"] == "split"
    recovered = tmp_path / "异常退出后"
    recovered.mkdir()
    (recovered / path.name).write_bytes(path.read_bytes())
    restored = build_window(storage_dir=recovered)
    qtbot.addWidget(restored, before_close_func=lambda item: item.dispose())
    restored.show()
    qtbot.waitUntil(lambda: restored.documents.active.canvas.view.transform().m11() == 1.5)
    assert [view.id for view in restored.documents.ordered()] == [left.id, right.id]
    assert restored.documents.active.id == left.id and len(restored.host.editor.groups) == 2


@pytest.mark.parametrize("delayed_file", [1, 2])
@pytest.mark.parametrize("outcome", ["complete", "error", "cancel"])
def test_slow_startup_preserves_saved_layout_until_all_documents_are_ready(qtbot, tmp_path, monkeypatch,
                                                                         delayed_file, outcome):
    """分别延迟首份和第二份导图，验证主题、侧栏尺寸及分屏在真实嵌套事件循环后恢复。"""
    directory = tmp_path / "布局配置"
    window = build_window(storage_dir=directory, restore=False)
    qtbot.addWidget(window)
    window.show()
    assert window.new_document(tmp_path / "第一份.mindmap")
    first = window.plugins.instance("mindmap").active_session
    assert window.new_document(tmp_path / "第二份.mindmap")
    second = window.plugins.instance("mindmap").active_session
    window.host.editor.split_token(second.id, Qt.Orientation.Horizontal)
    window.manager.set_theme("dark")
    window.manager.set_option("compact", True)
    window.plugins.instance("mindmap").show_settings_panel()
    snapshot = window.manager.capture_layout()
    snapshot["hosts"]["main"]["region_sizes"]["dock"].update(left=315, right=425, bottom=215)
    window.manager.restore_layout(snapshot)
    window.manager.activate_document(first.id)
    QApplication.processEvents()
    assert window.close()
    layout_path = directory / "workbench-state-v1.json"
    saved_bytes = layout_path.read_bytes()
    saved_layout = json.loads(saved_bytes)["layout"]["session"]
    started, release = Event(), Event()
    during_restore, reads = [], []
    original = loading.load_validated_document

    def delayed_reader(path):
        reads.append(path)
        if len(reads) == delayed_file:
            started.set()
            assert release.wait(5)
            if outcome == "error":
                raise MindMapError("测试读取失败")
        return original(path)

    def record_and_release():
        try:
            during_restore.append(layout_path.read_bytes())
            if outcome == "cancel":
                QApplication.activeModalWidget().findChild(QPushButton).click()
        finally:
            release.set()

    def watch_reader():
        if started.is_set():
            timer.stop()
            # 超过布局保存的 250 毫秒防抖周期，确保真正覆盖后台等待的时间窗口。
            QTimer.singleShot(400, record_and_release)

    monkeypatch.setattr(loading, "BACKGROUND_OPEN_BYTES", 0)
    monkeypatch.setattr(loading, "load_validated_document", delayed_reader)
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(watch_reader)
    timer.start()
    restored = None
    try:
        restored = build_window(storage_dir=directory, restore=True)
        qtbot.addWidget(restored)
        assert during_restore == [saved_bytes]
        restored.show()
        QApplication.processEvents()
        assert restored.manager.theme.name == "dark" and restored.manager.theme.compact
        assert restored.manager.states["map_settings"].visible
        assert not restored.manager.states["properties"].visible
        assert restored.host.region_sizes == saved_layout["hosts"]["main"]["region_sizes"]
        if outcome == "complete":
            assert len(restored.plugins.instance("mindmap").sessions) == 2 and restored.plugins.instance("mindmap").active_session.id == first.id
            assert restored.host.editor.snapshot() == saved_layout["hosts"]["main"]["editor"]
        else:
            remaining = second if delayed_file == 1 else first
            assert len(restored.plugins.instance("mindmap").sessions) == 1 and restored.plugins.instance("mindmap").active_session.id == remaining.id
        # 启动屏障释放后，用户的新调整仍须正常自动记忆。
        restored.manager.set_theme("light")
        qtbot.waitUntil(lambda: json.loads(layout_path.read_bytes())["layout"]["session"]["theme"] == "light")
    finally:
        release.set()
        timer.stop()
        if restored is not None:
            restored.close()
