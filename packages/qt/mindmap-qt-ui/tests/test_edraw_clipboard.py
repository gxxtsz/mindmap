"""按真实能力归属迁移的组件与行为回归。"""


import pytest
from mindmap_core import Theme
from mindmap_qt_ui.dialogs import EdrawPasteOptions, edraw_paste_options
from mindmap_qt_ui.ui_style import ui_colors
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QAbstractButton, QApplication, QDialog, QLabel, QPushButton, QWidget


@pytest.mark.parametrize("root_count", [1, 3])
def test_actual_dialog_defaults_switches_and_cancel(qtbot, root_count):
    canvas = QWidget()
    qtbot.addWidget(canvas)
    widget = canvas

    def inspect_dialog():
        dialog = QApplication.activeModalWidget()
        try:
            assert isinstance(dialog, QDialog) and dialog.windowTitle() == "粘贴亿图脑图"
            assert any("目标节点：<原目标>" in label.text() for label in dialog.findChildren(QLabel))
            append = dialog.findChild(QAbstractButton, "edraw_append")
            replace = dialog.findChild(QAbstractButton, "edraw_replace")
            keep = dialog.findChild(QPushButton, "edraw_keep_children")
            remove = dialog.findChild(QPushButton, "edraw_remove_children")
            descendants = dialog.findChild(QWidget, "edraw_descendants")
            confirm = dialog.findChild(QPushButton, "edraw_paste_confirm")
            assert append.palette().color(QPalette.ColorRole.WindowText) == QColor(Theme.dark().text)
            assert replace.isEnabled() == (root_count == 1)
            assert replace.isChecked() == (root_count == 1)
            assert keep.isEnabled() == (root_count == 1) and not keep.isChecked()
            assert remove.isChecked() and descendants.isVisible() == (root_count == 1)
            assert confirm.text() == ("替换粘贴" if root_count == 1 else "追加粘贴")
            append.setChecked(True)
            assert not keep.isEnabled() and not descendants.isVisible()
            assert remove.isChecked() and confirm.text() == "追加粘贴"
            if root_count == 1:
                replace.setChecked(True)
                assert keep.isEnabled() and not keep.isChecked()
                assert descendants.isVisible() and confirm.text() == "替换粘贴"
                keep.setChecked(True)
                assert not remove.isChecked()
        finally:
            dialog.accept()

    QTimer.singleShot(0, inspect_dialog)
    result = edraw_paste_options(widget, target_text="<原目标>", root_count=root_count, node_count=8,
                                 mode="replace", keep_children=False, theme=Theme.dark())
    assert result == EdrawPasteOptions("replace" if root_count == 1 else "append", root_count == 1)
    QTimer.singleShot(0, lambda: QApplication.activeModalWidget().reject())
    assert edraw_paste_options(widget, target_text="取消", root_count=1, node_count=1) is None


@pytest.mark.parametrize("dark", [False, True])
def test_choice_cards_show_full_selection_and_accept_padding_clicks(qtbot, dark):
    canvas = QWidget()
    qtbot.addWidget(canvas)
    """选择靠整块底色表达，整行留白可点，键盘仍能操作；避免仅检查逻辑勾选值。"""
    widget = canvas
    theme = Theme.dark() if dark else Theme()
    colors = ui_colors(theme)

    def sample(button):
        image = button.grab().toImage()
        ratio = image.devicePixelRatio()
        return image.pixelColor(round(10 * ratio), round(button.height() / 2 * ratio))

    def interact():
        dialog = QApplication.activeModalWidget()
        try:
            append = dialog.findChild(QAbstractButton, "edraw_append")
            replace = dialog.findChild(QAbstractButton, "edraw_replace")
            assert sample(append) == QColor(colors.selected)
            assert sample(replace) == QColor(colors.field)
            assert replace.accessibleName() == "替换当前主题" and replace.accessibleDescription()
            qtbot.mouseClick(replace, Qt.MouseButton.LeftButton, pos=QPoint(10, replace.height() - 10))
            assert replace.isChecked() and not append.isChecked()
            assert sample(replace) == QColor(colors.selected)
            append.setFocus()
            QApplication.processEvents()
            qtbot.keyClick(append, Qt.Key.Key_Space)
            assert append.isChecked() and not replace.isChecked()
            qtbot.keyClick(append, Qt.Key.Key_Down)
            assert replace.isChecked() and not append.isChecked()
        finally:
            dialog.accept()

    QTimer.singleShot(0, interact)
    assert edraw_paste_options(widget, target_text="目标", root_count=1, node_count=2530,
                               theme=theme) == EdrawPasteOptions("replace", True)
