"""使用独立场景导出，保证隐藏选择框并且不改写当前文档或编辑状态。"""

import math
import os
import tempfile
from pathlib import Path

from mindmap_core import Curve, LayoutRegistry, MindMapError
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QMarginsF, QRectF, QSize, QSizeF
from PySide6.QtGui import QColor, QImage, QPageSize, QPainter, QPdfWriter
from PySide6.QtSvg import QSvgGenerator, QSvgRenderer
from PySide6.QtWidgets import QGraphicsScene

from .graph_groups import GraphGroupItem, GraphStacking
from .items import NodeItem
from .rendering import BranchItem, VisualFactory


def export_document(
    document, path, *, layout_registry: LayoutRegistry, expand_all=False, scale=1.0,
    detail_page_resolver=None, layout_provider=None,
):
    """导出 PNG/SVG/PDF。矢量输出使用画布尺寸，PDF 为完整导图单页。

    scale 控制 PNG 分辨率；图片、文字和线条统一复用屏幕绘制逻辑。
    所有输出先在内存完成，再原子替换文件，失败不会截断原有目标。
    detail_page_resolver 是可选的只读共享详情解析函数，接收节点 ID 并返回
    （实际归属节点 ID，详情页 ID 或 None），供单页快照显示页外共享入口。
    省略时取节点自身引用；解析只影响图标与尺寸，不修改快照或保存文件内容。
    layout_provider 可按本次测量的尺寸提供只读布局。图页通过它传入分组框、
    节点位置和箭头几何。未传入时使用 layout_registry 计算页面布局。
    """
    target = Path(path).absolute()
    if not document.nodes:
        raise MindMapError("暂无可导出的想法内容")
    title = document.title if getattr(document, "kind", None) == "graph" else document.root.text
    extension = target.suffix.lower()
    if extension not in (".png", ".svg", ".pdf"):
        raise MindMapError("支持的图形导出格式为 PNG、SVG、PDF")
    if not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0:
        raise MindMapError("导出缩放必须是正有限数值")
    factory = VisualFactory()
    visuals, sizes = factory.measure(document, detail_page_resolver=detail_page_resolver)
    layout = (layout_provider(sizes) if layout_provider is not None else
              layout_registry.layout(document, sizes, expand_all=expand_all))
    scene = QGraphicsScene()
    scene.setBackgroundBrush(QColor(document.theme.background))
    graph = getattr(document, "graph", None)
    stacking = GraphStacking(graph) if graph is not None else None
    for key, frame in layout.group_frames.items():
        item = GraphGroupItem(frame, accent=document.theme.accent, border=document.theme.border,
                              background=document.theme.background)
        if stacking is not None:
            item.setZValue(stacking.frames[key])
        scene.addItem(item)
    for key, rect in layout.nodes.items():
        item = NodeItem(document.nodes[key], visuals[key], document.theme)
        item.group_title = key in layout.group_frames
        if stacking is not None:
            item.opaque_image = item.node.is_image_node
            item.setZValue(stacking.nodes[key])
        item.setPos(rect.x, rect.y)
        scene.addItem(item)
    for key, curve in layout.branches.items():
        item = BranchItem(curve)
        if stacking is not None:
            item.setZValue(stacking.edges[key])
        elif isinstance(curve, Curve):
            # 旧导出器在节点之后以默认层级绘制 Curve，边界接点的覆盖顺序也属于
            # 兼容外观；保留该顺序，新显式路径则使用图元默认的节点下方层级。
            item.setZValue(0)
        scene.addItem(item)
    bounds = layout.bounds
    source = QRectF(bounds.x, bounds.y, bounds.width, bounds.height)
    width, height = math.ceil(bounds.width), math.ceil(bounds.height)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    device = None
    if extension == ".png":
        width, height = math.ceil(width * scale), math.ceil(height * scale)
        if width * height > 40_000_000:
            raise MindMapError("PNG 超过四千万像素，请减小缩放比例或导出 SVG/PDF")
        device = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        if device.isNull():
            raise MindMapError("无法分配导出图片内存")
        device.fill(QColor(document.theme.background))
    elif extension == ".svg":
        # SVG 1.1 支持把导入图片的裁剪边界一起写入矢量文件。
        has_svg = any(isinstance(visuals[key].image, QSvgRenderer) for key in layout.nodes)
        version = QSvgGenerator.SvgVersion.Svg11 if has_svg else QSvgGenerator.SvgVersion.SvgTiny12
        device = QSvgGenerator(version)
        device.setOutputDevice(buffer)
        device.setSize(QSize(width, height))
        device.setViewBox(QRectF(0, 0, width, height))
        device.setTitle(title)
    else:
        device = QPdfWriter(buffer)
        device.setResolution(96)
        device.setPageSize(
            QPageSize(QSizeF(width * 25.4 / 96, height * 25.4 / 96), QPageSize.Unit.Millimeter)
        )
        device.setPageMargins(QMarginsF(0, 0, 0, 0))
        device.setTitle(title)
    painter = QPainter(device)
    if not painter.isActive():
        raise MindMapError("无法初始化导出绘图设备")
    try:
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        scene.render(painter, QRectF(0, 0, width, height), source)
    finally:
        painter.end()
    if extension == ".png" and not device.save(buffer, "PNG"):
        raise MindMapError("PNG 编码失败")
    # PDF 对象析构前会完成页尾；end() 已结束页面，此处释放写入器再读取缓冲区。
    device = None
    buffer.close()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(bytes(data))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError as exc:
        raise MindMapError(f"图形导出失败：{exc}") from exc
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
