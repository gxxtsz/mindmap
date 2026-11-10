"""按真实能力归属迁移的组件与行为回归。"""





from mindmap_core import BranchPath, BranchStyle, Curve, PathCommand, Theme
from mindmap_qt_render.rendering import BranchItem, branch_path
from mindmap_qt_ui.branch_styles import branch_style_icon
from PySide6.QtCore import QSize
from PySide6.QtGui import QPainterPath


def test_icons_and_path_commands_use_same_geometry(qapp):
    for theme in (Theme(), Theme.dark()):
        for kind in (None, *BranchStyle):
            icon = branch_style_icon(kind, theme)
            assert not icon.pixmap(QSize(54, 54)).isNull()
    branch = BranchPath(
        (0, 0),
        (20, 0),
        (
            PathCommand("M", ((0, 0),)),
            PathCommand("L", ((10, 10),)),
            PathCommand("C", ((10, 5), (15, 0), (20, 0))),
            PathCommand("Z"),
        ),
        "#123456",
        arrow=((20, 0), (15, -3), (15, 3)),
    )
    item = BranchItem(branch)
    assert isinstance(branch_path(branch), QPainterPath)
    assert item.boundingRect().contains(15, -3)
    item.set_branch(Curve(((0, 0), (5, 0), (15, 0), (20, 0)), "#123456"))
    assert not item.boundingRect().contains(15, -3)
