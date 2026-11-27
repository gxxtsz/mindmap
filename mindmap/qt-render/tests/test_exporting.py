"""静态渲染只依赖核心协议；无需安装内置布局即可导出三种图形。"""

from dataclasses import replace
from math import ceil

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
from PySide6.QtGui import QImage, QPainter
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


def _read_export_pixels(path, size):
    """回读实际导出文件，矢量格式按指定尺寸渲染后比较像素。"""
    if path.suffix == ".png":
        return QImage(str(path))
    if path.suffix == ".svg":
        svg = QSvgRenderer(str(path))
        assert svg.isValid()
        image = QImage(size, QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        svg.render(painter)
        painter.end()
        return image
    pdf = QPdfDocument()
    assert pdf.load(str(path)) == QPdfDocument.Error.None_ and pdf.pageCount() == 1
    image = pdf.render(0, size)
    pdf.close()
    return image


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
        (GraphEdge("c", "g1"),), {"a": (-180, -40), "c": (0, -350)}, (group,),
        edge_origins={GraphEdge("c", "g1"): (GraphEdge("c", "a"), GraphEdge("c", "b"))},
    ))
    titles, frames, arrows = [], [], []
    title_paint, frame_paint, branch_paint = NodeItem.paint, GraphGroupItem.paint, rendering.BranchItem.paint

    def title(item, painter, *args):
        if item.group_title:
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
    merged = _read_export_pixels(target, QSize(500, 500))
    assert not merged.isNull()
    # 同一条组级箭头有两条来源时，导出外观应与直接连到组上的箭头逐像素一致。
    engine.set_node_graph(owner, replace(page.graph, edge_origins={}))
    page = engine.page_document(graph_page_id(owner))
    direct = tmp_path / f"直接连线.{suffix}"
    export_document(page, direct, layout_registry=LayoutRegistry(), layout_provider=layout_provider)
    assert merged == _read_export_pixels(direct, QSize(500, 500))


@pytest.mark.parametrize("suffix", ["png", "svg", "pdf"])
def test_exported_pixels_follow_group_and_node_stacking_in_both_directions(qapp, tmp_path, suffix):
    engine = MindMapEngine()
    graph = NodeGraph((GraphNode("g", "组"), GraphNode("a", "甲"), GraphNode("n", "ABCDEFGH")),
                      groups=(GraphGroup("g", ("a",), (330, 180)),),
                      positions={"a": (0, 0), "n": (120, 24)})
    for front in ("g", "n"):
        engine.set_node_graph(engine.root_id, graph.raised_nodes([front]))
        page = engine.page_document(graph_page_id(engine.root_id))
        layout = None

        def layout_provider(sizes):
            nonlocal layout
            layout = graph_layout(page.graph, build_graph_layout(sizes, (), color=page.theme.accent, groups=graph.groups))
            return layout

        target = tmp_path / f"{front}.{suffix}"
        export_document(page, target, layout_registry=LayoutRegistry(), layout_provider=layout_provider)
        size = QSize(ceil(layout.bounds.width), ceil(layout.bounds.height))
        image = _read_export_pixels(target, size)
        # 组内空白压住卡片时整块同色，卡片在前时同一区域可以读到正文。
        box = layout.nodes["n"]
        x, y = round(box.x - layout.bounds.x), round(box.y - layout.bounds.y)
        colors = {image.pixel(i, j) for i in range(x + 6, x + int(box.width) - 6)
                  for j in range(y + 6, y + int(box.height) - 6)}
        if front == "g":
            assert len(colors) == 1
        else:
            assert len(colors) > 2
