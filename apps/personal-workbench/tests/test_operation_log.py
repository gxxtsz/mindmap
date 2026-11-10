"""操作日志的真实控件、事务摘要、文件结果、多会话归属及布局兼容验收。"""

import json

import pytest
from personal_workbench.app import build_window
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication
from workbench_docking import DockPosition, DockTarget


@pytest.fixture
def log_window(qtbot, tmp_path):
    """隔离配置与导图路径，清理阶段恢复失败注入，确保真实退出及资源释放。"""
    window = build_window(storage_dir=tmp_path / "配置", restore=False)

    def clean(item):
        for session in item.plugins.instance("mindmap").sessions.values():
            session.panels.properties.discard_pending()
            session.canvas.finish_edit(commit=False)
            session.files.writer = session.document._write
            session.engine.mark_saved()

    qtbot.addWidget(window, before_close_func=clean)
    window.resize(1200, 820)
    window.show()
    QApplication.processEvents()
    yield window
    if not window._disposed:
        clean(window)
        window.close()


def entries(window):
    """只读取得已呈现的日志，空占位文字不算一条操作记录。"""
    return window.operation_log.plain_text().splitlines()


def create(window, path):
    assert window.new_document(path)
    return window.plugins.instance("mindmap").active_session


@pytest.mark.parametrize("visible", [False, True])
def test_log_layout_and_theme_restore_without_log_history(log_window, tmp_path, qtbot, visible):
    """布局保留位置尺寸及显隐，主题切换不改内容；重启后不恢复日志文本。"""
    window = log_window
    window.record_operation("上一进程的独有记录")
    before = entries(window)
    window.manager.move("operation_log", DockTarget(DockPosition.RIGHT_BOTTOM))
    snapshot = window.manager.capture_layout()
    snapshot["hosts"]["main"]["region_sizes"]["dock"]["right"] = 405
    window.manager.restore_layout(snapshot)
    for theme in ("dark", "light"):
        window.manager.set_theme(theme)
        window.manager.set_option("compact", True)
        assert entries(window) == before
        assert window.operation_log.list_view.palette().color(QPalette.ColorRole.Text) == window.manager.theme.color("text-default")
    if not visible:
        window.manager.hide("operation_log")
    directory = window.manager.storage_dir
    assert window.close()
    assert "上一进程的独有记录" not in (directory / "workbench-layout-v1.json").read_text("utf-8")
    restored = build_window(storage_dir=directory)
    qtbot.addWidget(restored)
    restored.show()
    QApplication.processEvents()
    try:
        assert entries(restored) == []
        assert restored.manager.states["operation_log"].position == DockPosition.RIGHT_BOTTOM
        assert restored.manager.states["operation_log"].visible is visible
        assert restored.host.region_sizes["dock"]["right"] == 405
        assert restored.manager.settings.compact
        restored.manager.restore_default_layout()
        assert restored.manager.states["operation_log"].visible
        assert restored.manager.states["operation_log"].position == DockPosition.BOTTOM_LEFT
    finally:
        restored.close()


def test_old_layout_adds_visible_log_without_replacing_other_preferences(log_window, qtbot):
    """旧版本没有日志状态时补齐底部面板，继续恢复原来的明暗及侧栏偏好。"""
    window = log_window
    directory = window.manager.storage_dir
    snapshot = window.manager.capture_layout()
    snapshot["tools"] = [tool for tool in snapshot["tools"] if tool["id"] != "operation_log"]
    snapshot["active"] = "properties"
    snapshot["theme"] = "dark"
    snapshot["hosts"]["main"]["region_sizes"]["dock"]["right"] = 411
    assert window.close()
    (directory / "workbench-layout-v1.json").write_text(
        json.dumps({"schema_version": 1, "session": snapshot}), encoding="utf-8",
    )
    restored = build_window(storage_dir=directory)
    qtbot.addWidget(restored)
    restored.show()
    QApplication.processEvents()
    try:
        assert restored.manager.states["operation_log"].visible and restored.operation_log.isVisible()
        assert restored.manager.theme.name == "dark"
        assert restored.host.region_sizes["dock"]["right"] == 411
    finally:
        restored.close()
