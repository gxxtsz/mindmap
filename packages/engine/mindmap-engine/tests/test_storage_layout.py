"""文件往返、异常保护和布局几何测试。"""

from mindmap_engine import (
    MindMapDocument,
    Node,
    create_default_layout_registry,
    dumps,
    loads,
)


def test_large_depth_serialization_and_layout():
    document = MindMapDocument.new()
    current = document.root
    for i in range(1100):
        child = Node(f"深层节点 {i}")
        document.nodes[child.id] = child
        current.children.append(child.id)
        current = child
    restored = loads(dumps(document))
    assert len(create_default_layout_registry().layout(restored).nodes) == 1101
