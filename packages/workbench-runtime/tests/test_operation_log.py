"""通用操作日志的容量、选择、复制、焦点、显隐与控件生命周期。"""

import re

import pytest
from PySide6.QtCore import QItemSelectionModel, QPoint, Qt
from PySide6.QtWidgets import QAbstractItemView, QApplication
from workbench_plugin_mindmap.operation_log import describe_display
from workbench_runtime.operation_log import OperationLogPanel
from workbench_runtime.ui.docking import DockPosition


@pytest.fixture
def log_window(window):
    """复用仅装文本插件的通用窗口，日志能力不依赖具体编辑器。"""
    window.resize(1200, 820)
    QApplication.processEvents()
    return window


def entries(window):
    """只读取得已呈现的日志，空占位文字不算一条操作记录。"""
    return window.operation_log.plain_text().splitlines()


@pytest.mark.parametrize("entry", ["shortcut", "menu", "button"])
def test_log_is_available_without_document_and_toggle_reuses_panel(log_window, qtbot, entry):
    """空白页可通过三个入口切换日志，追加及清空均不改显隐或创建伪文档。"""
    window = log_window
    assert entries(window) == [] and not window.documents.views
    assert window.operation_log.list_view.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert window.manager.states["operation_log"].visible and window.operation_log.isVisible()
    assert window.manager.states["operation_log"].position == DockPosition.BOTTOM_LEFT
    assert window.host.region_sizes["dock"]["bottom"] == 260
    assert window.manager.is_tool_available("operation_log")
    assert window.manager.specs["operation_log"].shortcut == "Alt+5"
    assert not window.log_action.icon().isNull()

    def toggle():
        if entry == "shortcut":
            window.activateWindow()
            qtbot.keyClick(window, Qt.Key.Key_5, Qt.KeyboardModifier.AltModifier)
        elif entry == "menu":
            menu = window.main_menu()
            view = next(action.menu() for action in menu.actions() if action.text() == "视图")
            assert window.log_action in view.actions() and window.log_action.isEnabled()
            window.log_action.trigger()
            menu.deleteLater()
        else:
            button = next(button for rail in (window.host.left_rail, window.host.right_rail)
                          for identifier, button in rail._buttons.items() if identifier == "operation_log")
            qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        QApplication.processEvents()

    original = window.operation_log
    toggle()
    assert not original.isVisible() and not window.log_action.isChecked()
    window.record_operation("隐藏期间仍记录")
    assert len(entries(window)) == 1 and not original.isVisible()
    toggle()
    assert window.operation_log is original and original.isVisible() and window.log_action.isChecked()
    assert window.manager.documents == {}


def test_log_capacity_copy_selection_clear_and_single_line(qtbot, qapp):
    """容量按条目计算，特殊换行不能绕过上限；复制与清空不破坏行选择或系统内容。"""
    panel = OperationLogPanel()
    qtbot.addWidget(panel)
    panel.resize(700, 220)
    panel.show()
    assert not panel.copy_all_action.isEnabled() and not panel.clear_action.isEnabled()
    qapp.clipboard().setText("原剪贴板")
    panel.copy_all()
    assert qapp.clipboard().text() == "原剪贴板"
    for index in range(1002):
        panel.append_entry(f"记录{index:04d}\n第二行\u2029第三行", source="文件\n名.mindmap")
    lines = panel.plain_text().splitlines()
    assert len(lines) == panel.MAX_ENTRIES and panel.model.rowCount() == panel.MAX_ENTRIES
    assert "记录0002 第二行 第三行" in lines[0] and "记录1001" in lines[-1]
    assert re.match(r"\d{2}:\d{2}:\d{2} \[信息\] \[文件 名.mindmap\]", lines[0])
    index = panel.model.index(30, 0)
    panel.list_view.selectionModel().select(index, QItemSelectionModel.SelectionFlag.Select)
    selection = index.data()
    panel.copy_action.trigger()
    assert qapp.clipboard().text() == selection
    panel.copy_all_action.trigger()
    assert qapp.clipboard().text() == panel.plain_text()
    assert panel.list_view.selectionModel().selectedIndexes()[0].data() == selection
    with pytest.raises(ValueError, match="日志级别"):
        panel.append_entry("无效级别", level="debug")
    assert panel.model.rowCount() == panel.MAX_ENTRIES
    panel.clear_action.trigger()
    assert panel.model.rowCount() == 0 and not panel.clear_action.isEnabled()
    panel.append_entry("下一条", level="error")
    assert "[错误]" in panel.plain_text() and panel.model.rowCount() == 1


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("compact", [False, True])
def test_log_header_uses_icon_buttons_with_accessible_names(log_window, theme, compact):
    """真实停靠标题使用图标，主题和紧凑切换保留中文提示及复制清空行为。"""
    window = log_window
    window.manager.set_theme(theme)
    window.manager.set_option("compact", compact)
    window.record_operation("可复制的记录")
    buttons = window.manager.panel("operation_log").action_buttons
    assert len(buttons) == 2
    for button, action in zip(buttons, (window.operation_log.copy_all_action, window.operation_log.clear_action)):
        assert button.defaultAction() is action and not action.icon().isNull()
        assert button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
        assert button.accessibleName() == action.toolTip()
    buttons[0].click()
    assert QApplication.clipboard().text() == window.operation_log.plain_text()
    buttons[1].click()
    assert entries(window) == []


def test_keyboard_copy_keeps_complete_selected_records_in_time_order(qtbot, qapp):
    """多选可按 Ctrl+C 复制完整记录，点击顺序不影响时间顺序，Ctrl+A 继续可用。"""
    panel = OperationLogPanel()
    qtbot.addWidget(panel)
    panel.resize(900, 300)
    panel.show()
    panel.activateWindow()
    panel.list_view.setFocus()
    for index in range(8):
        panel.append_entry(f"操作{index}：完整说明")
    QApplication.processEvents()
    for row in (5, 1):
        panel.list_view.selectionModel().select(panel.model.index(row, 0), QItemSelectionModel.SelectionFlag.Select)
    qtbot.keyClick(panel.list_view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert qapp.clipboard().text() == "\n".join(panel.model.entries[row].plain_text for row in (1, 5))
    qtbot.keyClick(panel.list_view, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyClick(panel.list_view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert qapp.clipboard().text() == panel.plain_text()


def test_log_presentation_keeps_paths_in_details_and_copy_when_narrow(qtbot, qapp):
    """展示不堆叠完整路径，窄侧栏不撑出横向滚动；详情与复制保留原始记录。"""
    panel = OperationLogPanel()
    qtbot.addWidget(panel)
    path = r"E:\资料库\第二大脑.mindmap"
    title, detail = describe_display("打开导图：" + path)
    panel.append_entry("打开导图：" + path, source="第二大脑.mindmap", source_path=path, title=title, detail=detail)
    panel.append_entry("操作失败：详细原因" * 40, source="很长的文件名" * 10, level="error")
    panel.append_entry("页面提示：已返回主导图", level="warning")
    panel.resize(280, 200)
    panel.show()
    QApplication.processEvents()
    entry = panel.model.entries[0]
    assert entry.display_parts == ("打开导图", "") and entry.source_label == "第二大脑"
    assert path in panel.model.index(0, 0).data(Qt.ItemDataRole.ToolTipRole)
    assert not panel.grab().isNull() and panel.list_view.horizontalScrollBar().maximum() == 0
    panel.copy_all()
    assert path in qapp.clipboard().text() and "详细原因" in qapp.clipboard().text()


@pytest.mark.parametrize("at_capacity", [False, True])
def test_append_preserves_history_position_selection_and_focus(qtbot, qapp, at_capacity):
    """新增及淘汰旧记录时保留正在浏览的可见行、选区；在底部时才自动跟随。"""
    panel = OperationLogPanel()
    qtbot.addWidget(panel)
    panel.resize(700, 240)
    panel.show()
    for index in range(1000 if at_capacity else 80):
        panel.append_entry(f"历史记录{index}")
    QApplication.processEvents()
    scroll = panel.list_view.verticalScrollBar()
    assert scroll.value() == scroll.maximum()
    index = panel.model.index(30, 0)
    panel.list_view.selectionModel().select(index, QItemSelectionModel.SelectionFlag.Select)
    scroll.setValue(605)
    QApplication.processEvents()
    first = panel.list_view.indexAt(QPoint(0, 0)).data()
    selection = panel.list_view.selectionModel().selectedIndexes()[0].data()
    focus = qapp.focusWidget()
    panel.append_entry("追加后保持位置")
    assert panel.list_view.indexAt(QPoint(0, 0)).data() == first
    assert panel.list_view.selectionModel().selectedIndexes()[0].data() == selection
    assert qapp.focusWidget() is focus
    scroll.setValue(scroll.maximum())
    panel.append_entry("底部跟随")
    assert scroll.value() == scroll.maximum()
