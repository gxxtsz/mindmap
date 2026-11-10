"""同文件页面引用的目标选择器；页面路径用于显示，确认只返回根节点标识。"""

from collections import Counter

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from mindmap_qt_editor.theme import apply_ui_palette, dialog_styles


def choose_reference_root(parent, pages, *, current_root_id, colors):
    """显示可搜索的目标列表，返回确认的根节点 ID，取消时返回 None。

    pages 是已排除当前页的（根节点 ID、页面类型、完整路径）序列。同名同路径
    的条目另加顺序号；搜索只影响可见性，绝不从显示文字反推标识。此处不持有
    引擎或修改文档，调用方负责在模态对话框关闭后重新检查节点及页面是否有效。
    """
    dialog = QDialog(parent)
    dialog.setWindowTitle("更换引用页面" if current_root_id else "引用页面")
    dialog.resize(620, 440)
    layout = QVBoxLayout(dialog)
    search = QLineEdit(dialog)
    search.setObjectName("page_reference_search")
    search.setAccessibleName("查找引用页面")
    search.setPlaceholderText("搜索页面名称、类型或路径")
    layout.addWidget(search)

    tree = QTreeWidget(dialog)
    tree.setObjectName("page_reference_targets")
    tree.setAccessibleName("可引用的页面")
    tree.setHeaderLabels(["页面类型", "完整路径"])
    tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
    tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    tree.setRootIsDecorated(False)
    layout.addWidget(tree)
    empty = QLabel(dialog)
    empty.setWordWrap(True)
    layout.addWidget(empty)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
                              parent=dialog)
    accept = buttons.button(QDialogButtonBox.StandardButton.Ok)
    accept.setText("设置引用")
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
    layout.addWidget(buttons)

    role = Qt.ItemDataRole.UserRole
    totals = Counter((kind, path) for _, kind, path in pages)
    occurrences = Counter()
    entries = []
    for root_id, kind, path in pages:
        occurrences[kind, path] += 1
        if totals[kind, path] > 1:
            path = f"{path}（{occurrences[kind, path]}）"
        item = QTreeWidgetItem(tree, [kind, path])
        item.setData(0, role, root_id)
        item.setToolTip(1, path)
        entries.append((item, f"{kind} {path}".casefold()))
        if root_id == current_root_id:
            tree.setCurrentItem(item)

    def selected_root():
        item = tree.currentItem()
        return item.data(0, role) if item and item.isSelected() and not item.isHidden() else None

    def sync_selection():
        accept.setEnabled(selected_root() is not None)

    def filter_pages(value):
        tokens = value.casefold().split()
        visible = False
        for item, label in entries:
            matches = all(token in label for token in tokens)
            item.setHidden(not matches)
            visible |= matches
        empty.setText("没有匹配的页面" if pages else "当前文件里没有其他可引用的页面")
        empty.setVisible(not visible)
        sync_selection()

    def confirm(*_):
        if selected_root() is not None:
            dialog.accept()

    search.textChanged.connect(filter_pages)
    tree.itemSelectionChanged.connect(sync_selection)
    tree.itemDoubleClicked.connect(confirm)
    buttons.accepted.connect(confirm)
    buttons.rejected.connect(dialog.reject)
    apply_ui_palette(dialog, colors)
    dialog.setStyleSheet(dialog_styles(colors))
    search.setFocus()
    filter_pages("")
    try:
        return selected_root() if dialog.exec() == QDialog.DialogCode.Accepted else None
    finally:
        dialog.deleteLater()
