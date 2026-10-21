"""按真实能力归属迁移的组件与行为回归。"""




from mindmap_core import MindMapDocument, MindMapEngine, Node
from mindmap_qt_canvas import MindMapWidget
from mindmap_qt_panels import MindMapPanels


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
