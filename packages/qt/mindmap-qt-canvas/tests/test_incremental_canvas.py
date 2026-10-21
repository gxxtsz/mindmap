"""正式虚拟画布的像素、跨屏选择、逻辑导出及增量落点一致性。"""

from copy import deepcopy
from random import Random

import pytest
from mindmap_core import LayoutRegistry, MindMapDocument, MindMapEngine, Node, Theme
from mindmap_layouts import TreeLayout
from mindmap_layouts.incremental_result import IncrementalTreeLayout
from mindmap_qt_canvas import MindMapWidget
from mindmap_qt_canvas.dragging import _DropResolver
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication


def make_document(count=1101, *, wide=False, kind="round"):
    document = MindMapDocument.new()
    document.root.style = {"branch": {"kind": kind}}
    keys = [document.root_id]
    for index in range(1, count):
        node = Node(f"主题 {index}", style=deepcopy(document.root.style))
        document.nodes[keys[0 if wide else (index - 1) // 5]].children.append(node.id)
        document.nodes[node.id] = node
        keys.append(node.id)
    return document, keys


class LegacyTree(TreeLayout):
    """强制使用完整布局和完整图元，作为生产虚拟化路径的独立渲染参照。"""


def render(scene, area):
    image = QImage(640, 400, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
    scene.render(painter, QRectF(0, 0, 640, 400), area)
    painter.end()
    return bytes(image.constBits())


@pytest.mark.parametrize("direction", ["right", "left", "both", "down"])
@pytest.mark.parametrize("wide", [False, True])
def test_virtual_scene_matches_complete_scene_pixels(qtbot, direction, wide):
    document, keys = make_document(wide=wide)
    document.layout.direction = direction
    document.theme = Theme.dark() if wide else Theme()
    if wide:
        document.theme.palette = ["#883578cc", "#88289b79", "#88b076c2", "#88cf8b38"]
    registry = LayoutRegistry()
    registry.register(LegacyTree(), copy_inputs=False)
    actual = MindMapWidget(MindMapEngine(document), toolbar=False)
    expected = MindMapWidget(MindMapEngine(document), toolbar=False, layout_registry=registry)
    try:
        for canvas in (actual, expected):
            qtbot.addWidget(canvas)
            canvas.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            canvas.resize(850, 550)
            canvas.show()
        QApplication.processEvents()
        assert actual._virtualized and len(actual.node_items) < len(document.nodes) // 2
        for key in (document.root_id, keys[len(keys) // 2], keys[-1]):
            center = actual.layout_result.nodes[key].center
            area = QRectF(center[0] - 320, center[1] - 200, 640, 400)
            for canvas in (actual, expected):
                canvas.select_nodes([])
                canvas.view.centerOn(QPointF(*center))
                canvas._viewport_changed()
            pixels = render(actual.scene, area)
            reference = render(expected.scene, area)
            # 局部/世界坐标的浮点重组只允许极少量边缘抗锯齿差异，缺线或缺节点会显著超出。
            changed = sum(abs(a - b) > 8 for a, b in zip(pixels, reference, strict=True))
            assert changed < 100, (direction, wide, key, changed)
    finally:
        actual.dispose()
        expected.dispose()


@pytest.mark.parametrize("direction", ["right", "left", "both", "down"])
@pytest.mark.parametrize("scale", [0.1, 1, 4])
def test_virtual_drop_query_matches_complete_fixed_index(direction, scale):
    document, keys = make_document()
    document.layout.direction = direction
    sizes = {key: (120 + index % 4 * 10, 38 + index % 3 * 4)
             for index, key in enumerate(keys)}
    full_layout = TreeLayout().layout(document, sizes)
    virtual_layout = IncrementalTreeLayout(document, sizes).result()
    reference = _DropResolver(document, full_layout, [keys[-1]], None)
    actual = _DropResolver(document, virtual_layout, [keys[-1]], None)
    reference.rebuild(full_layout.nodes, scale=scale)
    actual.rebuild(virtual_layout.nodes, scale=scale)
    randomizer = Random(419)
    points = [QPointF(0, 0)]
    for _ in range(35):
        key = randomizer.choice(keys)
        rect = full_layout.nodes[key]
        points.extend((QPointF(*rect.center), QPointF(rect.x - 13 / scale, rect.y),
                       QPointF(rect.right + 16 / scale, rect.bottom + 12 / scale)))
        children = document.nodes[key].children
        if len(children) >= 2:
            a, b = full_layout.nodes[children[0]], full_layout.nodes[children[1]]
            points.append(QPointF((a.center[0] + b.center[0]) / 2, (a.center[1] + b.center[1]) / 2))
    for point in points:
        expected, direct = reference.resolve(point, scale)
        got, got_direct = actual.resolve(point, scale)
        assert got_direct == direct
        if expected is None:
            assert got is None
        else:
            assert got is not None
            assert (got.proposal, got.reference, got.position) == (
                expected.proposal, expected.reference, expected.position
            ), (direction, scale, point, got, expected)


def test_offscreen_selection_history_and_complete_export(qtbot, tmp_path):
    document, keys = make_document()
    canvas = MindMapWidget(MindMapEngine(document))
    qtbot.addWidget(canvas)
    canvas.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    canvas.resize(900, 600)
    canvas.show()
    QApplication.processEvents()
    try:
        source, target = keys[-1], keys[5]
        canvas.select_nodes([source])
        assert canvas.selected_node_ids() == [source] and source in canvas.node_items
        canvas.engine.move_nodes([source], target)
        assert canvas.engine.undo() and canvas.engine.redo()
        canvas.engine.set_expanded(target, False)
        assert source not in canvas.layout_result.nodes
        assert canvas.engine.undo() and source in canvas.layout_result.nodes
        snapshot = canvas.layout_result
        old = snapshot.nodes[source]
        canvas.engine.update_node(source, text="远端完整导出验证")
        assert snapshot.nodes[source] == old
        canvas.center_root()
        canvas.select_nodes([])
        assert source not in canvas.node_items
        path = tmp_path / "完整.svg"
        canvas.export(path)
        assert "远端完整导出验证" in path.read_text(encoding="utf-8")
    finally:
        canvas.dispose()
