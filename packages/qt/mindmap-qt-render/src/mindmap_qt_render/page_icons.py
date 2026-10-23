"""页面类型的共享矢量图形；节点、路径栏及导出使用同一份定义。"""

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF
from PySide6.QtGui import QColor, QPainter
from PySide6.QtSvg import QSvgRenderer

_PAGE_ICON_BODIES = {
    "main": '<path d="m3 10 9-7 9 7M5 9v12h14V9M9 21v-8h6v8"/>',
    "detail": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>',
    "submap": (
        '<rect x="9" y="2" width="6" height="6" rx="1"/>'
        '<rect x="2" y="16" width="6" height="6" rx="1"/>'
        '<rect x="16" y="16" width="6" height="6" rx="1"/>'
        '<path d="M12 8v4M5 16v-4h14v4"/>'
    ),
}


def page_icon_body(kind: str) -> str:
    """取得页面图标的 SVG 图形体，供共享 UI 的多状态图标工厂复用。

    kind 只接受 main、detail、submap；返回值使用 24×24 坐标，调用方负责
    SVG 外壳、颜色、两单位描边及圆角端点。未知类别显式抛出 ValueError，
    不把数据错误静默显示成另一种页面。本接口不创建 Qt 对象或读取文档。
    """
    try:
        return _PAGE_ICON_BODIES[kind]
    except KeyError:
        raise ValueError(f"页面图标类别不存在：{kind}") from None


@lru_cache(maxsize=128)
def _renderer(kind: str, color: str) -> QSvgRenderer:
    """缓存只读的静态 SVG，避免每个节点或每一帧重新解析相同图形。"""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'fill="none" stroke="{color}" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round">{page_icon_body(kind)}</svg>'
    )
    return QSvgRenderer(QByteArray(svg.encode("utf-8")))


def paint_page_icon(painter: QPainter, kind: str, rect: QRectF, color: QColor | str) -> None:
    """在给定局部矩形绘制页面图标，供画布、拖动影像及矢量导出共用。

    rect 是图形本身的区域，不包含按钮命中留白；颜色由调用方根据节点背景
    或界面色板决定。此接口在 Qt GUI 线程调用，保留画笔状态，不修改文档，
    使用矢量渲染以适配高 DPI、SVG 和 PDF，不引入交互组件或上层依赖。
    """
    painter.save()
    try:
        _renderer(kind, QColor(color).name()).render(painter, rect)
    finally:
        painter.restore()
