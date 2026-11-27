"""静态渲染只依赖核心协议；无需安装内置布局即可导出三种图形。"""

import pytest
from mindmap_core import (
    GraphEdge,
    GraphGroup,
    GraphNode,
    LayoutConfig,
    LayoutInfo,
    LayoutRegistry,
    LayoutResult,
    MindMapDocument,
    MindMapEngine,
    NodeGraph,
    Rect,
)
from mindmap_core.layouts.graph import build_graph_layout, graph_layout
from mindmap_core.node_pages import graph_page_id
from mindmap_qt_render import NodeItem, export_document, rendering
from mindmap_qt_render.graph_groups import GraphGroupItem
from PySide6.QtCore import QSize
from PySide6.QtGui import QImage
from PySide6.QtPdf import QPdfDocument
from PySide6.QtSvg import QSvgRenderer


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


@pytest.mark.parametrize("suffix", ["png", "svg", "pdf"])
def test_graph_exports_draw_real_group_title_and_frame_endpoint(qapp, tmp_path, monkeypatch, suffix):
    engine = MindMapEngine()
    owner = engine.root_id
    group = GraphGroup("g1", ("a", "b"))
    engine.set_node_graph(owner, NodeGraph(
        (GraphNode("a", "甲"), GraphNode("b", "乙"), GraphNode("c", "入口"), GraphNode("g1", "核心模块")),
        (GraphEdge("c", "g1"),), {"a": (-180, -40)}, (group,),
    ))
    titles, frames, arrows = [], [], []
    title_paint, frame_paint, branch_paint = NodeItem.paint, GraphGroupItem.paint, rendering.BranchItem.paint

    def title(item, painter, *args):
        titles.append(item.node.text)
        title_paint(item, painter, *args)

    def frame(item, painter, *args):
        frames.append(item.scenePos())
        frame_paint(item, painter, *args)

    def branch(item, painter, *args):
        arrows.append(item._branch.arrow)
        branch_paint(item, painter, *args)

    monkeypatch.setattr(NodeItem, "paint", title)
    monkeypatch.setattr(GraphGroupItem, "paint", frame)
    monkeypatch.setattr(rendering.BranchItem, "paint", branch)
    target = tmp_path / f"分组.{suffix}"
    page = engine.page_document(graph_page_id(owner))

    def layout_provider(sizes):
        automatic = build_graph_layout(sizes, page.graph.edges, color=page.theme.accent, groups=page.graph.groups)
        return graph_layout(page.graph, automatic)

    export_document(page, target, layout_registry=LayoutRegistry(), layout_provider=layout_provider)
    assert "核心模块" in titles and len(frames) == len(arrows) == 1 and arrows[0] is not None
    if suffix == "png":
        assert not QImage(str(target)).isNull()
    elif suffix == "svg":
        assert QSvgRenderer(str(target)).isValid()
    else:
        pdf = QPdfDocument()
        assert pdf.load(str(target)) == QPdfDocument.Error.None_
        assert pdf.pageCount() == 1 and not pdf.render(0, QSize(500, 500)).isNull()
        pdf.close()
