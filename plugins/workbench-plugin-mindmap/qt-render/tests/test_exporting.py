"""静态渲染只依赖核心协议；无需安装内置布局即可导出三种图形。"""

import pytest
from mindmap_core import (
    LayoutConfig,
    LayoutInfo,
    LayoutRegistry,
    LayoutResult,
    MindMapDocument,
    MindMapEngine,
    Rect,
)
from mindmap_qt_render import export_document, rendering


class SingleNodeLayout:
    """受控的单节点测试布局，不借用任何内置算法。"""

    info = LayoutInfo("single", "单节点测试", LayoutConfig(algorithm_id="single"))

    def validate_config(self, config):
        pass

    def layout(self, document, measurements=None, **options):
        width, height = measurements[document.root_id]
        rect = Rect(0, 0, width, height)
        return LayoutResult(nodes={document.root_id: rect}, bounds=rect.padded(30))


@pytest.mark.parametrize("suffix", ["png", "svg", "pdf"])
def test_export_with_only_core_layout_protocol(qapp, tmp_path, suffix):
    document = MindMapDocument.new("无内置布局的渲染")
    document.layout = LayoutConfig(algorithm_id="single")
    registry = LayoutRegistry()
    registry.register(SingleNodeLayout())
    target = tmp_path / f"导图.{suffix}"
    export_document(document, target, layout_registry=registry)
    data = target.read_bytes()
    assert len(data) > 100
    if suffix == "png":
        assert data.startswith(b"\x89PNG")
    elif suffix == "pdf":
        assert data.startswith(b"%PDF")
    else:
        assert b"<svg" in data


@pytest.mark.parametrize("suffix", ["png", "svg", "pdf"])
def test_all_graphic_exports_draw_both_page_icons(qapp, tmp_path, monkeypatch, suffix):
    """三种实际导出均调用共享矢量绘图，不创建交互控件或丢弃页面标记。"""
    engine = MindMapEngine()
    engine.create_detail_page(engine.root_id)
    engine.create_submap(engine.root_id)
    document = engine.document.page_view()
    document.layout = LayoutConfig(algorithm_id="single")
    registry = LayoutRegistry()
    registry.register(SingleNodeLayout())
    calls = []
    original = rendering.paint_page_icon

    def painted(painter, kind, rect, color):
        calls.append((kind, rect.width(), rect.height()))
        original(painter, kind, rect, color)

    monkeypatch.setattr(rendering, "paint_page_icon", painted)
    target = tmp_path / f"附页标记.{suffix}"
    export_document(document, target, layout_registry=registry)
    assert target.stat().st_size > 100
    assert {kind for kind, _, _ in calls} == {"detail", "submap"}
    assert all(width == height == 16 for _, width, height in calls)
