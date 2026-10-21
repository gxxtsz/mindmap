"""按真实能力归属迁移的组件与行为回归。"""


import pytest
from mindmap_core import LayoutConfig, MindMapDocument, MindMapEngine, Node
from mindmap_layouts import TreeLayout
from mindmap_qt_canvas import MindMapWidget
from mindmap_qt_canvas.dragging import _distance, _DropResolver, _RegionIndex
from PySide6.QtCore import QPointF, QRectF


def document_with_nodes(count):
    """直接建模避开造数据期间的编辑通知，每个标题不同以覆盖内容缓存临界点。"""
    document = MindMapDocument.new("大图回归")
    keys = [document.root_id]
    for index in range(1, count):
        node = Node(f"不同内容的节点 {index}")
        document.nodes[keys[(index - 1) // 5]].children.append(node.id)
        document.nodes[node.id] = node
        keys.append(node.id)
    return document, keys


def test_large_drop_displays_final_layout_without_blocking_reflow_animation(qtbot):
    document, keys = document_with_nodes(600)
    engine = MindMapEngine(document)
    canvas = MindMapWidget(engine)
    qtbot.addWidget(canvas)
    canvas.resize(1200, 800)
    canvas.show()
    qtbot.wait(1)
    try:
        source, target = keys[-1], keys[-3]
        canvas.select_nodes([source])
        canvas.view.centerOn(QPointF(*canvas.layout_result.nodes[target].center))
        control = canvas.view.node_drag
        assert control.begin(QPointF(*canvas.layout_result.nodes[source].center))
        end = canvas.view.mapFromScene(QPointF(*canvas.layout_result.nodes[target].center))
        drop = control.finish(end)
        assert drop is not None and control.commit(drop)
        assert control._reflow is None
        assert source in engine.get_node(target).children
        assert all(
            item.pos() == QPointF(canvas.layout_result.nodes[key].x, canvas.layout_result.nodes[key].y)
            for key, item in canvas.node_items.items()
        )
        assert engine.undo() and engine.document == document
    finally:
        canvas.dispose()


def test_spatial_query_keeps_all_nearby_regions_and_their_original_priority():
    regions = [QRectF(index * 37 - 600, index % 7 * 93 - 300, 95, 48) for index in range(60)]
    regions.extend([QRectF(-20000, -20000, 40000, 40000), QRectF(256, -256, 0, 128)])
    index = _RegionIndex(enumerate(regions))
    for point in (QPointF(-256, -256), QPointF(256, -128), QPointF(0, 0), QPointF(1000, 256)):
        for radius in (0, 24, 240):
            nearby = [key for key, _ in index.nearby(point, radius)]
            required = {key for key, rect in enumerate(regions) if _distance(point, rect) <= radius}
            assert required <= set(nearby)
            assert nearby == sorted(nearby)


@pytest.mark.parametrize("direction", ["right", "left", "both", "down"])
@pytest.mark.parametrize("scale", [0.1, 1, 4])
def test_lazy_large_map_targets_match_complete_target_semantics(direction, scale):
    document, keys = document_with_nodes(600)
    document.layout = LayoutConfig(direction=direction)
    layout = TreeLayout().layout(document)
    lazy = _DropResolver(document, layout, [keys[-1]], None)
    full = _DropResolver(document, layout, [keys[-1]], None)
    lazy.rebuild(layout.nodes)
    full.rebuild(layout.nodes)
    gaps = [target for target, _ in full._gap_index.entries]
    full._gap_index = None
    full.targets = []
    full._append_node_targets(full.node_regions.items())
    full.targets.extend(gaps)
    for key in keys[::29]:
        rect = layout.nodes[key]
        for point in (
            QPointF(*rect.center), QPointF(rect.right + 23 / scale, rect.center[1]),
            QPointF(rect.center[0], rect.bottom + 12 / scale),
        ):
            actual, direct = lazy.resolve(point, scale)
            expected, expected_direct = full.resolve(point, scale)
            assert direct == expected_direct and actual == expected
