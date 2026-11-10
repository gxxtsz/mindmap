"""纯图片的画布辅助效果；不参与正式视觉、资源、布局或图形导出。"""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

# 少量外缘叠绘形成轻投影，不对整张大图片建立模糊缓冲区。
_SHADOW_RADIUS = 6
_SHADOW_OFFSET = 2


def image_shadow_bounds(rect: QRectF) -> QRectF:
    """返回图片及其轻投影的联合绘制范围，不改变图片的测量尺寸。

    rect 为图元或场景坐标中的图片矩形，调用方须与实际绘制使用同一坐标系。
    返回独立 QRectF；无正常业务异常，不保留输入引用，不依赖文档生命周期。
    仅由所属 GUI 线程在存活画布上使用，供 Qt 正确清理移动前后的投影区域。
    """
    shadow = rect.translated(0, _SHADOW_OFFSET).adjusted(
        -_SHADOW_RADIUS, -_SHADOW_RADIUS, _SHADOW_RADIUS, _SHADOW_RADIUS)
    return rect.united(shadow)


def paint_image_shadow(painter: QPainter, rect: QRectF, background: QColor) -> None:
    """绘制只位于图片外侧的柔和投影，返回 None，不覆盖透明图片的内部区域。

    painter 是本次 GUI 绘制借用的画笔，rect 使用其当前逻辑坐标；background 为
    画布实际底色，深色画布采用微亮的投影以区分同色图片。投影随画布正常缩放，
    不建立图片副本、缓存或 Qt 效果对象，也不改变布局、资源、选择及历史。
    仅在画布或缩放预览中调用，不用于静态导出；恢复画笔状态、不持有任何参数，
    无正常业务异常。调用方须保证画笔和所属视图在本次 GUI 调用期间有效。
    """
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    clip.setFillRule(Qt.FillRule.OddEvenFill)
    clip.addRect(image_shadow_bounds(rect))
    clip.addRect(rect)
    painter.setClipPath(clip, Qt.ClipOperation.IntersectClip)
    painter.setPen(Qt.PenStyle.NoPen)
    color = QColor("#dce6f2" if background.lightnessF() < 0.5 else "#253044")
    for radius in range(_SHADOW_RADIUS, 0, -1):
        color.setAlpha(_SHADOW_RADIUS + 1 - radius)
        painter.setBrush(color)
        shadow = rect.translated(0, _SHADOW_OFFSET).adjusted(-radius, -radius, radius, radius)
        painter.drawRoundedRect(shadow, radius, radius)
    painter.restore()


def paint_image_hover(painter: QPainter, rect: QRectF, background: QColor) -> None:
    """绘制一个屏幕逻辑像素宽的临时悬停轮廓，返回 None，不绘制底色。

    painter、rect、background 的坐标和生命周期要求同 paint_image_shadow；
    轮廓按实际画布明暗调整颜色，仅供未选中的悬停纯图片调用。使用装饰画笔
    保持不同画布倍率下的线宽，不改变图片尺寸、资源、文档及历史，不参与导出。
    本次 GUI 调用结束即归还画笔状态，不缓存借用对象，无正常业务异常。
    """
    painter.save()
    color = QColor("#dce6f2" if background.lightnessF() < 0.5 else "#253044")
    color.setAlpha(120 if background.lightnessF() < 0.5 else 95)
    pen = QPen(color, 1)
    pen.setCosmetic(True)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(rect)
    painter.restore()
