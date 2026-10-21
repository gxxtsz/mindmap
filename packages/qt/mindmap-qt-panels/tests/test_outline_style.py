"""检查大纲的真实分支像素、原生命中、主题及滚动，避免仅断言样式字符串。"""

import pytest
from mindmap_core import MindMapDocument, MindMapEngine, Theme
from mindmap_qt_panels.outline import OutlinePanel
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication


@pytest.fixture
def outline(qtbot, panel_host_factory):
    """同一树同时覆盖有子节点、连续叶节点及子树之后的同级节点。"""
    engine = MindMapEngine(MindMapDocument.new("根主题"))
    group = engine.add_child(engine.root_id, "父主题")
    first = engine.add_child(group, "第一个叶主题")
    last = engine.add_child(group, "最后一个叶主题")
    sibling = engine.add_child(engine.root_id, "同级叶主题")
    canvas = panel_host_factory(engine)
    qtbot.addWidget(canvas)
    tree = OutlinePanel(canvas)
    qtbot.addWidget(tree)
    tree.resize(300, 220)
    tree.show()
    tree.expandAll()
    tree.clearSelection()
    QApplication.processEvents()
    yield tree, engine, (group, first, last, sibling)
    canvas.dispose()


def image_color(image, point):
    """采样实际设备像素，兼容系统缩放及独立 DPI 验收进程。"""
    ratio = image.devicePixelRatio()
    return image.pixelColor(round(point.x() * ratio), round(point.y() * ratio))


def marker_center(tree, index):
    """从 Qt 正式行几何求标记中心，不能以固定视口位置代替滚动后的命中坐标。"""
    rect = tree.visualRect(index)
    x = (
        rect.right() + 1 + tree.indentation() // 2
        if tree.isRightToLeft()
        else rect.left() - tree.indentation() // 2
    )
    return QPoint(x, rect.top() + rect.height() // 2)


@pytest.mark.parametrize("rtl", [False, True])
@pytest.mark.parametrize("theme", [Theme(), Theme.dark()])
def test_guides_continue_through_last_descendant_and_stop_at_sibling(outline, rtl, theme):
    """末尾孩子也保留祖先实线；下一棵子树的空白处不能残留上一层导线。"""
    tree, engine, (group, first, last, sibling) = outline
    engine.set_theme(theme)
    tree.setLayoutDirection(Qt.LayoutDirection.RightToLeft if rtl else Qt.LayoutDirection.LeftToRight)
    tree.clearSelection()
    QApplication.processEvents()
    model = tree.tree_model
    image = tree.viewport().grab().toImage()
    root_marker = marker_center(tree, model.index_for(engine.root_id))
    parent_marker = marker_center(tree, model.index_for(group))
    leaf_marker = marker_center(tree, model.index_for(first))
    guide = QColor("#45474b" if theme == Theme.dark() else "#dfe2e8")
    for key in (first, last):
        rect = tree.visualRect(model.index_for(key))
        for x in (root_marker.x(), parent_marker.x()):
            assert image_color(image, QPoint(x, rect.top() + 1)) == guide
            assert image_color(image, QPoint(x, rect.bottom() - 1)) == guide
    sibling_rect = tree.visualRect(model.index_for(sibling))
    background = QColor("#2b2d30" if theme == Theme.dark() else "#f5f6f8")
    assert image_color(image, QPoint(parent_marker.x(), sibling_rect.top() + 1)) == background
    assert image_color(image, leaf_marker) == QColor("#92959e" if theme == Theme.dark() else "#80848d")
    assert tree.visualRect(model.index_for(first)).height() == 28
    assert abs(parent_marker.x() - root_marker.x()) == 16
    assert tree.font().pixelSize() == 12


@pytest.mark.parametrize("rtl", [False, True])
def test_custom_triangle_keeps_native_click_and_keyboard_expansion(outline, qtbot, rtl):
    """绘制不接管事件：三角的点击及左右键仍由 Qt 改变面板展开，不改文档。"""
    tree, engine, (group, first, last, sibling) = outline
    tree.setLayoutDirection(Qt.LayoutDirection.RightToLeft if rtl else Qt.LayoutDirection.LeftToRight)
    QApplication.processEvents()
    index = tree.tree_model.index_for(group)
    before, revision = engine.document, engine.revision
    qtbot.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=marker_center(tree, index))
    assert not tree.isExpanded(index)
    qtbot.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=marker_center(tree, index))
    assert tree.isExpanded(index)
    tree.setCurrentIndex(index)
    qtbot.keyClick(tree, Qt.Key.Key_Right if rtl else Qt.Key.Key_Left)
    assert not tree.isExpanded(index)
    qtbot.keyClick(tree, Qt.Key.Key_Left if rtl else Qt.Key.Key_Right)
    assert tree.isExpanded(index)
    assert engine.document == before and engine.revision == revision


@pytest.mark.parametrize("theme", [Theme(), Theme.dark()])
def test_leaf_click_selects_node_and_selection_uses_soft_theme_fill(outline, qtbot, theme):
    """圆点所在行正常选择，明暗主题的柔和选中态与悬停、分支底色保持区分。"""
    tree, engine, (group, first, last, sibling) = outline
    engine.set_theme(theme)
    index = tree.tree_model.index_for(first)
    calls = []
    tree.nodes_selected.connect(calls.append)
    qtbot.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=marker_center(tree, index))
    assert calls[-1] == [first]
    assert tree.selectionModel().isSelected(index)
    QApplication.processEvents()
    image = tree.viewport().grab().toImage()
    rect = tree.visualRect(index)
    dark = theme == Theme.dark()
    assert image_color(image, QPoint(rect.right() - 10, rect.center().y())) == QColor(
        "#374251" if dark else "#dfe9f8"
    )
    assert image_color(image, marker_center(tree, index)) == QColor("#92959e" if dark else "#80848d")
    tree.clearSelection()
    qtbot.mouseMove(tree.viewport(), rect.center())
    QApplication.processEvents()
    image = tree.viewport().grab().toImage()
    assert image_color(image, QPoint(rect.right() - 10, rect.center().y())) == QColor(
        "#393b40" if dark else "#f1f2f5"
    )


def test_pixel_scroll_and_theme_refresh_preserve_tree_state(outline):
    """滚动一逻辑像素后标记同步移动；明暗切换不重建模型、不丢失展开或多选。"""
    tree, engine, (group, first, last, sibling) = outline
    for number in range(18):
        engine.add_child(engine.root_id, f"追加主题 {number}")
    tree.expandAll()
    tree.sync_selection([first, last])
    tree.verticalScrollBar().setValue(0)
    QApplication.processEvents()
    model = tree.model()
    index = tree.tree_model.index_for(first)
    before = marker_center(tree, index)
    tree.verticalScrollBar().setValue(1)
    QApplication.processEvents()
    assert marker_center(tree, index).y() == before.y() - 1
    # 单独刷新主题，避免把引擎主题变更的画布定位规则混入视图状态断言。
    for theme in (Theme.dark(), Theme()):
        tree.set_theme(theme)
        QApplication.processEvents()
        assert tree.model() is model
        assert tree.isExpanded(tree.tree_model.index_for(group))
        assert {tree.tree_model.node_id(item) for item in tree.selectionModel().selectedRows()} == {first, last}
        assert tree.verticalScrollBar().value() == 1
        image = tree.viewport().grab().toImage()
        expected = QColor("#92959e" if theme == Theme.dark() else "#80848d")
        assert image_color(image, marker_center(tree, index)) == expected


def test_branch_hover_grows_arrow_and_leaving_restores_it(outline, qtbot):
    """只在鼠标位于当前展开区域时放大三角，移到正文或离开视口立即还原。"""
    tree, engine, (group, first, last, sibling) = outline
    index = tree.tree_model.index_for(group)

    def marker_pixels():
        """统计分支格的深色像素，实际比较标记面积，不依赖内部悬停字段。"""
        image = tree.viewport().grab().toImage()
        marker = marker_center(tree, index)
        return sum(
            image_color(image, marker + QPoint(x, y)).lightness() < 180
            for x in range(-6, 7) for y in range(-6, 7)
        )

    for expanded in (False, True):
        tree.setExpanded(index, expanded)
        qtbot.mouseMove(tree.viewport(), tree.visualRect(index).center())
        QApplication.processEvents()
        normal = marker_pixels()
        qtbot.mouseMove(tree.viewport(), marker_center(tree, index))
        QApplication.processEvents()
        assert marker_pixels() > normal
        QApplication.sendEvent(tree.viewport(), QEvent(QEvent.Type.Leave))
        QApplication.processEvents()
        assert marker_pixels() == normal
