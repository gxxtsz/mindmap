"""大图回归：排版不会随缓存容量失效，试算不会污染正式视觉，保存不重建界面。"""

from copy import deepcopy

import pytest
from mindmap_engine import LayoutConfig, MindMapDocument, MindMapEngine, Node, TreeLayout
from mindmap_qt import MindMapPanels, MindMapWidget, rendering
from mindmap_qt.dragging import _distance, _DropResolver, _RegionIndex
from mindmap_qt.rendering import VisualFactory
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


def test_text_layout_survives_more_than_4096_distinct_nodes(qapp, monkeypatch):
    document, keys = document_with_nodes(4100)
    calls = []
    original = rendering.make_text

    def measured(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(rendering, "make_text", measured)
    factory = VisualFactory()
    first, _ = factory.measure(document)
    assert len(calls) == 4100
    second, _ = factory.measure(deepcopy(document))
    assert len(calls) == 4100
    assert all(second[key] is first[key] for key in keys)
    document.nodes[keys[-1]].text = "确实修改正文"
    third, _ = factory.measure(document)
    assert len(calls) == 4101 and third[keys[-1]] is not first[keys[-1]]


def test_rejected_candidate_does_not_supply_unchanged_committed_visuals(qapp):
    document, keys = document_with_nodes(20)
    factory = VisualFactory()
    committed, _ = factory.measure(document)
    candidate = deepcopy(document)
    candidate.nodes[keys[1]].text = "很长的候选内容" * 15
    trial, _ = factory.measure(candidate)
    assert trial[keys[1]] is not committed[keys[1]]
    # 试算被拒绝后修改另一节点；增量基线必须是正式视觉，不能使用最近的试算缓存。
    actual = deepcopy(document)
    actual.nodes[keys[-1]].text = "另一次正式修改"
    updated, _ = factory.measure(
        actual, previous_document=document, previous_visuals=committed, changed_node_ids=[keys[-1]],
    )
    assert updated[keys[1]] is committed[keys[1]]
    actual.theme.accent = "#ff6600"
    themed, _ = factory.measure(
        actual, previous_document=document, previous_visuals=committed, changed_node_ids=[],
    )
    assert themed[document.root_id] is not committed[document.root_id]


def test_panels_and_saved_event_do_not_request_extra_document_copies(qtbot):
    class CountedEngine(MindMapEngine):
        reads = 0

        @property
        def document(self):
            self.reads += 1
            return super().document

    document, keys = document_with_nodes(30)
    engine = CountedEngine(document)
    canvas = MindMapWidget(engine)
    qtbot.addWidget(canvas)
    panels = MindMapPanels(canvas)
    try:
        engine.reads = 0
        canvas.select_nodes([keys[-1]])
        assert engine.reads == 0
        engine.update_node(keys[-1], text="正式修改")
        assert engine.reads == 0
        snapshot, visual = canvas._document, canvas.node_items[keys[-1]].visual
        engine.mark_saved()
        assert engine.reads == 0 and canvas._document is snapshot
        assert canvas.node_items[keys[-1]].visual is visual
        assert panels.outline.tree_model.document.nodes[keys[-1]].text == "正式修改"
    finally:
        panels.dispose()
        canvas.dispose()


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
