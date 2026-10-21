"""增量服务由实现包公开准入，覆写入口可靠回退完整布局。"""

import pytest
from mindmap_core import LayoutRegistry, MindMapDocument
from mindmap_layouts import (
    TreeLayout,
    create_default_layout_registry,
    create_incremental_layout,
    supports_incremental_layout,
)


def test_default_service_returns_stable_read_only_mappings():
    document = MindMapDocument.new()
    sizes = {document.root_id: (100, 40)}
    registry = create_default_layout_registry()
    assert supports_incremental_layout(registry, "tree")
    service = create_incremental_layout(registry, document, sizes)
    result = service.result()
    nodes, branches = dict(result.nodes), dict(result.branches)
    nodes.clear()
    assert document.root_id in result.nodes and not branches
    with pytest.raises(TypeError):
        result.nodes[document.root_id] = result.nodes[document.root_id]


@pytest.mark.parametrize("changed", [
    "registry_class", "algorithm_class", "registry_method", "algorithm_method",
    "registry_class_method", "algorithm_class_method",
])
def test_replaced_layout_dispatch_has_no_implicit_incremental_service(changed, monkeypatch):
    class CustomRegistry(LayoutRegistry):
        def layout(self, *args, **kwargs):
            return super().layout(*args, **kwargs)

    class CustomTree(TreeLayout):
        pass

    registry = CustomRegistry() if changed == "registry_class" else LayoutRegistry()
    algorithm = CustomTree() if changed == "algorithm_class" else TreeLayout()
    registry.register(algorithm, copy_inputs=False)
    if changed == "registry_method":
        monkeypatch.setattr(registry, "layout", lambda *args, **kwargs: None)
    if changed == "algorithm_method":
        monkeypatch.setattr(algorithm, "layout", lambda *args, **kwargs: None)
    if changed == "registry_class_method":
        monkeypatch.setattr(LayoutRegistry, "layout", lambda *args, **kwargs: None)
    if changed == "algorithm_class_method":
        monkeypatch.setattr(TreeLayout, "layout", lambda *args, **kwargs: None)
    document = MindMapDocument.new()
    assert not supports_incremental_layout(registry, "tree")
    assert create_incremental_layout(registry, document, {document.root_id: (100, 40)}) is None
