"""正式画布的样式选择、临时预览、事务和多种导出的集成验收。"""

from dataclasses import replace
from math import ceil

import pytest
from mindmap_engine import (
    BranchPath,
    BranchStyle,
    Curve,
    LayoutConfig,
    LayoutRegistry,
    MindMapEngine,
    MindMapError,
    PathCommand,
    Theme,
    TreeLayout,
    create_default_layout_registry,
    load_document,
    save_document,
)
from mindmap_qt import MindMapWidget
from mindmap_qt.branch_styles import branch_style_icon
from mindmap_qt.dialogs import NodePropertiesResult
from mindmap_qt.items import NodeItem
from mindmap_qt.properties import NodePropertiesForm
from mindmap_qt.rendering import BranchItem, VisualFactory, branch_path
from PySide6.QtCore import QMimeData, QPoint, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen
from PySide6.QtPdf import QPdfDocument
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QDialogButtonBox, QGraphicsScene, QTabWidget


@pytest.fixture
def canvas(qtbot):
    engine = MindMapEngine()
    engine.set_layout(LayoutConfig(direction="right"))
    first = engine.add_child(engine.root_id, "分支甲")
    children = [engine.add_child(first, text) for text in ("上级", "中级", "下级")]
    widget = MindMapWidget(engine)
    qtbot.addWidget(widget)
    widget.resize(900, 600)
    widget.show()
    widget.select_nodes([first])
    engine.mark_saved()
    yield widget, first, children
    widget.dispose()


def test_grid_preview_is_isolated_and_close_restores(canvas, qtbot, monkeypatch):
    widget, first, children = canvas
    before = widget.engine.document
    revision = widget.engine.revision
    paths = {key: item.path() for key, item in widget.branch_items.items()}
    visuals = {key: item.visual for key, item in widget.node_items.items()}
    original_measure = widget.factory.measure
    calls = []

    def measure(document):
        calls.append(document)
        return original_measure(document)

    monkeypatch.setattr(widget.factory, "measure", measure)
    widget.branch_menu.popup(widget.mapToGlobal(QPoint(80, 80)))
    widget.branch_menu.buttons[BranchStyle.ARROW].entered.emit()
    assert widget._preview_layout is not None
    assert all(isinstance(widget._preview_layout.branches[key], BranchPath) for key in children)
    assert (
        widget.engine.document == before and widget.engine.revision == revision and not widget.engine.is_dirty
    )
    assert calls == []
    assert all(widget.node_items[key].visual is value for key, value in visuals.items())
    widget.branch_menu.close()
    assert widget._preview_layout is None
    assert all(widget.branch_items[key].path() == path for key, path in paths.items())
    assert widget.engine.document == before


def test_click_applies_once_and_arrow_reset_is_clean(canvas, qtbot):
    widget, first, children = canvas
    before = widget.engine.document
    revision = widget.engine.revision
    widget.branch_menu.popup(widget.mapToGlobal(QPoint(80, 80)))
    widget.branch_menu.buttons[BranchStyle.ARROW2].entered.emit()
    qtbot.mouseClick(widget.branch_menu.buttons[BranchStyle.ARROW2], Qt.MouseButton.LeftButton)
    assert widget._preview_layout is None and not widget.branch_menu.isVisible()
    assert widget.engine.revision == revision + 1
    for key in children:
        assert widget.layout_result.branches[key].arrow is not None
        assert not widget.branch_items[key]._arrow.isEmpty()
    assert widget.engine.undo() and widget.engine.document == before
    assert all(widget.branch_items[key]._arrow.isEmpty() for key in children)
    widget.set_branch_style("arrow")
    widget.set_branch_style("line")
    assert all(widget.branch_items[key]._arrow.isEmpty() for key in children)
    widget.set_branch_style(None)
    assert all(isinstance(widget.layout_result.branches[key], Curve) for key in children)


def test_scope_mixed_selection_and_new_node_style(canvas):
    widget, first, children = canvas
    widget.engine.set_branch_style([children[0]], "arc", scope="level")
    assert widget.branch_menu.summary.text() == "多种样式"
    widget.branch_menu.scope.setCurrentIndex(widget.branch_menu.scope.findData("level"))
    assert widget.branch_menu.summary.text() == "默认曲线"
    widget.set_branch_style("round", scope="level")
    assert widget.engine.get_node(first).style["branch"]["kind"] == "round"
    assert widget.engine.get_node(children[0]).style["branch"]["kind"] == "arc"
    assert "branch" not in widget.engine.get_node(children[1]).style
    new = widget.engine.add_child(first)
    assert widget.engine.get_node(new).style["branch"]["kind"] == "round"


@pytest.mark.parametrize(
    "transition", ["selection", "document", "layout", "readonly", "edit", "dispose", "focus", "undo", "saved"]
)
def test_preview_cancelled_on_state_change(canvas, transition):
    widget, first, children = canvas
    widget.preview_branch_style("arrow")
    assert widget._preview_layout is not None
    if transition == "selection":
        widget.select_nodes([children[0]])
    elif transition == "document":
        widget.engine.update_node(first, text="修改内容")
    elif transition == "layout":
        widget.set_layout(LayoutConfig(direction="down"))
    elif transition == "readonly":
        widget.set_editable(False)
    elif transition == "edit":
        widget.begin_edit(first)
    elif transition == "focus":
        widget.focus_node(first)
    elif transition == "undo":
        widget.engine.undo()
    elif transition == "saved":
        widget.engine.mark_saved()
    else:
        widget.dispose()
    assert widget._preview_layout is None
    if transition in ("readonly", "edit"):
        assert not widget.action("branch_style").isEnabled()


def test_no_selection_uses_focused_topic_or_root(canvas):
    widget, first, children = canvas
    widget.select_nodes([])
    widget.set_branch_style("round", scope="level")
    assert widget.engine.document.root.style["branch"]["kind"] == "round"
    assert "branch" not in widget.engine.get_node(first).style
    widget.focus_node(first)
    widget.select_nodes([])
    widget.set_branch_style("arrow", scope="level")
    assert widget.engine.get_node(first).style["branch"]["kind"] == "arrow"
    assert all(widget.layout_result.branches[key].arrow is not None for key in children)
    assert all("branch" not in widget.engine.get_node(key).style for key in children)


def test_widget_clipboard_keeps_explicit_branch_styles(canvas, qapp):
    """正式专用剪贴板经格式编码后仍保留各级独立样式，粘贴不套用目标父主题配置。"""
    widget, first, children = canvas
    widget.set_branch_style("round")
    widget.engine.set_branch_style([children[0]], "arrow2", scope="level")
    clipboard = qapp.clipboard()
    original = QMimeData()
    mime = clipboard.mimeData()
    if mime is not None:
        for fmt in mime.formats():
            original.setData(fmt, mime.data(fmt))
    try:
        widget.copy()
        widget.select_nodes([widget.engine.root_id])
        pasted = widget.paste()[0]
        assert widget.engine.get_node(pasted).style["branch"]["kind"] == "round"
        nested = widget.engine.get_node(pasted).children[0]
        assert widget.engine.get_node(nested).style["branch"]["kind"] == "arrow2"
        widget.engine.set_branch_style([pasted], "line")
        assert widget.engine.get_node(first).style["branch"]["kind"] == "round"
    finally:
        if qapp.platformName() == "offscreen":
            clipboard.clear()
        else:
            clipboard.setMimeData(original)
        qapp.processEvents()


def test_invalid_style_and_guard_failure_preserve_state(canvas, qtbot):
    widget, first, _ = canvas
    before, revision = widget.engine.document, widget.engine.revision
    selected = widget.selected_node_ids()
    for kind, scope in (("unknown", "subtree"), ("round", "invalid")):
        with pytest.raises(MindMapError):
            widget.set_branch_style(kind, scope=scope)
        assert widget.engine.document == before and widget.engine.revision == revision
        assert widget.selected_node_ids() == selected
    errors = []
    widget.error_occurred.connect(errors.append)
    widget.engine.before_operation = lambda name, args: name != "set_branch_style"
    widget.branch_menu.popup(widget.mapToGlobal(QPoint(80, 80)))
    qtbot.mouseClick(widget.branch_menu.buttons[BranchStyle.ROUND], Qt.MouseButton.LeftButton)
    assert errors and not widget.branch_menu.isVisible()
    assert widget.engine.document == before and widget.branch_menu.summary.text() == "默认曲线"


def test_custom_layout_capability_and_old_curves(qtbot):
    class CustomLayout(TreeLayout):
        info = replace(
            TreeLayout.info,
            id="custom",
            name="自定义",
            default_config=LayoutConfig(algorithm_id="custom"),
            supports_branch_styles=False,
        )

        def validate_config(self, config):
            config.validate()

        def layout(self, document, measurements=None, **kwargs):
            document.layout.algorithm_id = "tree"
            return TreeLayout().layout(document, measurements, **kwargs)

    registry = create_default_layout_registry()
    registry.register(CustomLayout())
    widget = MindMapWidget(layout_registry=registry)
    qtbot.addWidget(widget)
    widget.set_layout(CustomLayout.info.default_config)
    assert not widget.action("branch_style").isEnabled()
    with pytest.raises(MindMapError, match="不支持"):
        widget.set_branch_style("round")
    widget.dispose()


@pytest.mark.parametrize("kind", list(BranchStyle))
@pytest.mark.parametrize("suffix", ["png", "svg", "pdf"])
@pytest.mark.parametrize("direction", ["right", "left", "both", "down"])
def test_all_styles_export_committed_geometry(canvas, tmp_path, kind, suffix, direction):
    widget, first, children = canvas
    widget.set_layout(LayoutConfig(direction=direction))
    widget.set_branch_style(kind)
    for key in children:
        assert widget.scene.sceneRect().contains(widget.branch_items[key].boundingRect())
    original = widget.engine.document
    target = tmp_path / f"{kind}.{suffix}"
    widget.export(target)
    assert widget.engine.document == original and target.stat().st_size > 300
    if suffix == "png":
        assert not QImage(str(target)).isNull()
    elif suffix == "svg":
        assert QSvgRenderer(str(target)).isValid()
    else:
        pdf = QPdfDocument()
        assert pdf.load(str(target)) == QPdfDocument.Error.None_
        assert pdf.pageCount() == 1
        assert not pdf.render(0, QSize(500, 500)).isNull()
        pdf.close()


def test_export_during_preview_and_file_roundtrip(canvas, tmp_path):
    widget, first, _ = canvas
    widget.set_branch_style("round")
    widget.export(tmp_path / "正式.png")
    before, revision = widget.engine.document, widget.engine.revision
    widget.preview_branch_style("arrow")
    widget.export(tmp_path / "预览期间.png")
    save_document(widget.engine.document, tmp_path / "带样式.mindmap")
    assert QImage(str(tmp_path / "正式.png")) == QImage(str(tmp_path / "预览期间.png"))
    assert load_document(tmp_path / "带样式.mindmap") == before
    assert widget.engine.revision == revision


def test_legacy_curve_export_keeps_original_pixels(canvas, tmp_path):
    """用原四点曲线描画流程作为基准，同时检查控制点、画笔和接点覆盖顺序。"""
    widget, _, _ = canvas
    document = widget.engine.document
    visuals, sizes = VisualFactory().measure(document)
    layout = widget.layout_registry.layout(document, sizes)
    scene = QGraphicsScene()
    scene.setBackgroundBrush(QColor(document.theme.background))
    for key, rect in layout.nodes.items():
        item = NodeItem(document.nodes[key], visuals[key], document.theme)
        item.setPos(rect.x, rect.y)
        scene.addItem(item)
    for curve in layout.branches.values():
        assert isinstance(curve, Curve)
        path = QPainterPath(QPointF(*curve.points[0]))
        path.cubicTo(*(QPointF(*point) for point in curve.points[1:]))
        scene.addPath(path, QPen(QColor(curve.color), 1.8))
    bounds = layout.bounds
    width, height = ceil(bounds.width), ceil(bounds.height)
    expected = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    expected.fill(QColor(document.theme.background))
    painter = QPainter(expected)
    painter.setRenderHints(
        QPainter.RenderHint.Antialiasing
        | QPainter.RenderHint.TextAntialiasing
        | QPainter.RenderHint.SmoothPixmapTransform
    )
    scene.render(
        painter, QRectF(0, 0, width, height), QRectF(bounds.x, bounds.y, bounds.width, bounds.height)
    )
    painter.end()
    widget.export(tmp_path / "默认曲线.png")
    actual = QImage(str(tmp_path / "默认曲线.png")).convertToFormat(expected.format())
    assert actual == expected


def test_properties_style_change_single_undo_and_unchanged_descendants(canvas, monkeypatch):
    widget, first, children = canvas
    widget.engine.set_branch_style([children[0]], "arc", scope="level")
    before = widget.engine.document
    patch = {"text": "新文本", "style": {**widget.engine.get_node(first).style, "font_size": 19}}
    monkeypatch.setattr(
        "mindmap_qt.widget.node_properties",
        lambda *args, **kwargs: NodePropertiesResult(patch, None, "subtree", False),
    )
    widget.edit_properties()
    assert widget.engine.get_node(children[0]).style["branch"]["kind"] == "arc"
    assert widget.engine.undo() and widget.engine.document == before
    monkeypatch.setattr(
        "mindmap_qt.widget.node_properties",
        lambda *args, **kwargs: NodePropertiesResult(patch, BranchStyle.ROUND, "subtree", True),
    )
    widget.edit_properties()
    assert widget.engine.get_node(first).text == "新文本"
    assert all(widget.engine.get_node(key).style["branch"]["kind"] == "round" for key in children)
    assert widget.engine.undo() and widget.engine.document == before


def test_property_dialog_has_icons_and_cancel_is_nonmutating(canvas, qtbot):
    widget, first, _ = canvas
    before = widget.engine.document

    def inspect_dialog():
        dialog = next(child for child in widget.findChildren(QDialog) if child.isVisible())
        combo = dialog.findChild(QComboBox, "branch_style")
        assert combo.count() == 12 and not combo.itemIcon(1).isNull()
        assert dialog.findChild(QTabWidget) is None
        combo.setCurrentIndex(5)
        dialog.reject()

    QTimer.singleShot(0, inspect_dialog)
    widget.edit_properties()
    assert widget.engine.document == before


def test_property_dialog_accepts_style_and_branch_together(canvas, qtbot):
    widget, first, children = canvas
    before, revision = widget.engine.document, widget.engine.revision

    def accept_dialog():
        dialog = next(child for child in widget.findChildren(QDialog) if child.isVisible())
        form = dialog.findChild(NodePropertiesForm)
        form.fields["style.font_size"].setValue(18)
        combo = dialog.findChild(QComboBox, "branch_style")
        combo.setCurrentIndex(combo.findData("arrow2"))
        buttons = dialog.findChild(QDialogButtonBox)
        qtbot.mouseClick(buttons.button(QDialogButtonBox.StandardButton.Ok), Qt.MouseButton.LeftButton)

    QTimer.singleShot(0, accept_dialog)
    widget.edit_properties()
    assert widget.engine.revision == revision + 1
    assert widget.engine.get_node(first).style["branch"]["kind"] == "arrow2"
    assert widget.engine.get_node(first).style["font_size"] == 18
    assert all(widget.layout_result.branches[key].arrow is not None for key in children)
    assert widget.engine.undo() and widget.engine.document == before


def test_property_dialog_keeps_unconfigured_defaults_and_content(canvas, qtbot):
    """单页弹窗确定但没有修改时，不写默认字体、宽度或节点内容。"""
    widget, _, _ = canvas
    root = widget.engine.root_id
    widget.engine.update_node(
        root, note="备注", tags=["原标签"], hyperlink="https://example.com", text_format="markdown"
    )
    widget.select_nodes([root])
    before, revision = widget.engine.document, widget.engine.revision

    def accept_unchanged():
        dialog = next(child for child in widget.findChildren(QDialog) if child.isVisible())
        form = dialog.findChild(NodePropertiesForm)
        assert "text" not in form.fields and "note" not in form.fields
        assert form.fields["style.font_size"].value() == 13
        assert form.fields["style.bold"].isChecked()
        assert not form.width_enabled.isChecked()
        dialog.accept()

    QTimer.singleShot(0, accept_unchanged)
    widget.edit_properties()
    assert widget.engine.document == before and widget.engine.revision == revision


@pytest.mark.parametrize("accepted", [False, True])
def test_property_dialog_grid_waits_for_confirm(canvas, qtbot, accepted):
    widget, first, _ = canvas
    before, revision = widget.engine.document, widget.engine.revision

    def choose_in_dialog():
        dialog = next(child for child in widget.findChildren(QDialog) if child.isVisible())
        form = dialog.findChild(NodePropertiesForm)
        picker = form.fields["branch.kind"]
        form.scroll.ensureWidgetVisible(picker)
        QApplication.processEvents()
        qtbot.mouseClick(picker, Qt.MouseButton.LeftButton)
        assert picker.popup_menu.isVisible()
        qtbot.mouseClick(picker.popup_menu.buttons[BranchStyle.ROUND], Qt.MouseButton.LeftButton)
        assert picker.currentData() == "round" and not picker.popup_menu.isVisible()
        assert widget.engine.document == before and widget.engine.revision == revision
        dialog.accept() if accepted else dialog.reject()

    QTimer.singleShot(0, choose_in_dialog)
    widget.edit_properties()
    if accepted:
        assert widget.engine.get_node(first).style["branch"]["kind"] == "round"
        assert widget.engine.undo() and widget.engine.document == before
    else:
        assert widget.engine.document == before and widget.engine.revision == revision


def test_grid_keyboard_preview_and_escape_restores(canvas, qtbot):
    widget, _, _ = canvas
    before = widget.engine.document
    menu = widget.branch_menu
    menu.popup(widget.mapToGlobal(QPoint(80, 80)))
    first = menu.buttons[BranchStyle.LINE]
    first.setFocus()
    qtbot.keyClick(first, Qt.Key.Key_Down)
    target = menu.buttons[BranchStyle.ROUND]
    assert target.hasFocus() and widget._preview_layout is not None
    qtbot.keyClick(target, Qt.Key.Key_Escape)
    assert not menu.isVisible() and widget._preview_layout is None
    assert widget.engine.document == before


def test_invalid_candidate_geometry_rejected_before_commit(qtbot):
    """支持样式的扩展布局若返回非法路径，组件在正式操作之前拒绝候选文档。"""

    class BrokenLayout(TreeLayout):
        def layout(self, document, measurements=None, **kwargs):
            result = super().layout(document, measurements, **kwargs)
            if "branch" in document.root.style:
                key = document.root.children[0]
                result.branches[key] = replace(result.branches[key], width=float("nan"))
            return result

    registry = LayoutRegistry()
    registry.register(BrokenLayout())
    engine = MindMapEngine()
    child = engine.add_child(engine.root_id)
    widget = MindMapWidget(engine, layout_registry=registry)
    qtbot.addWidget(widget)
    widget.select_nodes([engine.root_id, child])
    engine.mark_saved()
    before, revision = engine.document, engine.revision
    selection = widget.selected_node_ids()
    with pytest.raises(MindMapError, match="线宽"):
        widget.set_branch_style("round")
    assert engine.document == before and engine.revision == revision and not engine.is_dirty
    assert widget.selected_node_ids() == selection and widget._preview_layout is None
    widget.dispose()


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
    assert item._arrow.isEmpty()
