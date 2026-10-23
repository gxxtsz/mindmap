"""按真实能力归属迁移的组件与行为回归。"""


import pytest
from mindmap_core import Theme
from mindmap_qt_ui.dialogs import EdrawPasteOptions, edraw_paste_options
from mindmap_qt_ui.ui_style import ui_colors
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QAbstractButton, QApplication, QDialog, QLabel, QPushButton, QWidget


@pytest.mark.parametrize("root_count", [1, 3])
@pytest.mark.parametrize("replace_root", [False, True])
def test_actual_dialog_defaults_switches_and_cancel(qtbot, root_count, replace_root):
    """根节点和后代选择独立记忆，追加及多来源隐藏两组处理选项，取消不返回结果。"""
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
            root_options = dialog.findChild(QWidget, "edraw_root_options")
            replace_root_button = dialog.findChild(QPushButton, "edraw_replace_root")
            keep_root = dialog.findChild(QPushButton, "edraw_keep_root")
            keep = dialog.findChild(QPushButton, "edraw_keep_children")
            remove = dialog.findChild(QPushButton, "edraw_remove_children")
            descendants = dialog.findChild(QWidget, "edraw_descendants")
            confirm = dialog.findChild(QPushButton, "edraw_paste_confirm")
            assert append.palette().color(QPalette.ColorRole.WindowText) == QColor(Theme.dark().text)
            assert replace.isEnabled() == (root_count == 1)
            assert replace.isChecked() == (root_count == 1)
            assert keep.isEnabled() == (root_count == 1) and not keep.isChecked()
            assert remove.isChecked() and descendants.isVisible() == (root_count == 1)
            assert root_options.isVisible() == (root_count == 1)
            assert replace_root_button.isEnabled() == (root_count == 1)
            assert replace_root_button.isChecked() == replace_root
            assert keep_root.isChecked() == (not replace_root)
            assert confirm.text() == ("替换粘贴" if root_count == 1 else "追加粘贴")
            append.setChecked(True)
            assert not keep.isEnabled() and not descendants.isVisible()
            assert not root_options.isVisible() and not keep_root.isEnabled()
            assert replace_root_button.isChecked() == replace_root
            assert remove.isChecked() and confirm.text() == "追加粘贴"
            if root_count == 1:
                replace.setChecked(True)
                assert keep.isEnabled() and not keep.isChecked()
                assert descendants.isVisible() and confirm.text() == "替换粘贴"
                assert root_options.isVisible() and keep_root.isEnabled()
                assert replace_root_button.isChecked() == replace_root
                keep_root.setChecked(replace_root)
                replace_root_button.setChecked(not replace_root)
                assert remove.isChecked() and not keep.isChecked()
                keep.setChecked(True)
                assert not remove.isChecked()
                assert replace_root_button.isChecked() == (not replace_root)
        finally:
            dialog.accept()

    QTimer.singleShot(0, inspect_dialog)
    result = edraw_paste_options(widget, target_text="<原目标>", root_count=root_count, node_count=8,
                                 mode="replace", keep_children=False, replace_root=replace_root,
                                 theme=Theme.dark())
    assert result == EdrawPasteOptions("replace" if root_count == 1 else "append", root_count == 1,
                                      not replace_root if root_count == 1 else replace_root)
    QTimer.singleShot(0, lambda: QApplication.activeModalWidget().reject())
    assert edraw_paste_options(widget, target_text="取消", root_count=1, node_count=1) is None


@pytest.mark.parametrize("dark", [False, True])
def test_choice_cards_show_full_selection_and_accept_padding_clicks(qtbot, dark):
    """选择靠整块底色表达，整行留白可点，键盘仍能操作；新增按钮默认替换根节点。"""
    canvas = QWidget()
    qtbot.addWidget(canvas)
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
            root_replace = dialog.findChild(QPushButton, "edraw_replace_root")
            assert root_replace.isChecked() and sample(root_replace) == QColor(colors.accent)
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


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("keep_children", [False, True])
@pytest.mark.parametrize("replace_root", [False, True])
def test_root_and_descendants_buttons_update_hint(qtbot, dark, keep_children, replace_root):
    """在真实弹窗内点击四种组合，核对说明、互斥状态和选中背景，兼容明暗主题。"""
    parent = QWidget()
    qtbot.addWidget(parent)
    theme = Theme.dark() if dark else Theme()
    colors = ui_colors(theme)

    def interact():
        dialog = QApplication.activeModalWidget()
        try:
            root_replace = dialog.findChild(QPushButton, "edraw_replace_root")
            root_keep = dialog.findChild(QPushButton, "edraw_keep_root")
            keep = dialog.findChild(QPushButton, "edraw_keep_children")
            remove = dialog.findChild(QPushButton, "edraw_remove_children")
            root_choice = root_replace if replace_root else root_keep
            qtbot.mouseClick(root_choice, Qt.MouseButton.LeftButton)
            qtbot.mouseClick(keep if keep_children else remove, Qt.MouseButton.LeftButton)
            assert root_replace.isChecked() == replace_root and root_keep.isChecked() == (not replace_root)
            assert keep.isChecked() == keep_children and remove.isChecked() == (not keep_children)
            assert root_choice.accessibleName() == root_choice.text() and root_choice.accessibleDescription()
            hint = dialog.findChild(QLabel, "edraw_paste_result_hint").text()
            assert ("使用复制内容替换当前主题文字。" if replace_root
                    else "保留当前主题文字和格式，仅导入复制内容的子树。") in hint
            assert ("保留已有分支，新分支追加在后。" if keep_children else "移除已有分支") in hint
            image = root_choice.grab().toImage()
            ratio = image.devicePixelRatio()
            assert image.pixelColor(round(10 * ratio), round(root_choice.height() / 2 * ratio)) == QColor(
                colors.accent
            )
        finally:
            dialog.accept()

    QTimer.singleShot(0, interact)
    result = edraw_paste_options(parent, target_text="当前主题", root_count=1, node_count=4,
                                 mode="replace", theme=theme)
    assert result == EdrawPasteOptions("replace", keep_children, replace_root)
