"""从导图内的资源字节加载可反复绘制的静态 SVG。"""

from base64 import b64decode
from binascii import Error as Base64Error
from math import isfinite
from urllib.parse import unquote_to_bytes

from mindmap_core import MindMapError
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSizeF, Qt, QXmlStreamReader
from PySide6.QtGui import QImageReader
from PySide6.QtSvg import QSvgRenderer, QtSvg


def _check_embedded_image(reference: str) -> None:
    """确认 Qt 将从 SVG 字节内读取位图，避免通过链接读取导图以外的文件。"""
    header, separator, payload = reference.partition(",")
    if not separator or not header.startswith("data:"):
        raise MindMapError("SVG 引用了外部图像，请先将图像嵌入 SVG")
    data = unquote_to_bytes(payload)
    if header.endswith(";base64"):
        try:
            data = b64decode(b"".join(data.split()), validate=True)
        except Base64Error as error:
            raise MindMapError("SVG 内嵌图像的编码无效") from error
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    kind = bytes(QImageReader.imageFormat(buffer))
    if not kind or kind.startswith(b"svg"):
        raise MindMapError("SVG 内嵌图像必须是可识别的位图")


def load_svg(data: bytes) -> tuple[QSvgRenderer, QSizeF]:
    """校验自包含 SVG，返回 GUI 线程持有的渲染器及逻辑尺寸。"""
    contents = QByteArray(data)
    buffer = QBuffer()
    buffer.setData(contents)
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QXmlStreamReader(buffer)
    started = False
    keep_aspect = True
    while not reader.atEnd():
        reader.readNext()
        if not reader.isStartElement():
            continue
        name, attributes = reader.name(), reader.attributes()
        if not started:
            if name != "svg":
                raise MindMapError("SVG 文件内容无效")
            started = True
            keep_aspect = "none" not in attributes.value("preserveAspectRatio").split()
        if name in ("image", "feImage"):
            for attribute in ("href", "xlink:href"):
                reference = attributes.value(attribute).strip()
                if reference:
                    _check_embedded_image(reference)
    if not started or reader.hasError():
        raise MindMapError("SVG 文件内容无效")

    renderer = QSvgRenderer()
    renderer.setOptions(QtSvg.Option.DisableAnimations)
    if not renderer.load(contents):
        raise MindMapError("SVG 文件无法渲染")
    if keep_aspect:
        renderer.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
    size = QSizeF(renderer.defaultSize())
    if size.isEmpty():
        size = renderer.viewBoxF().size()
    width, height = size.width(), size.height()
    if not isfinite(width) or not isfinite(height) or min(width, height) <= 0:
        raise MindMapError("SVG 图像尺寸无效")
    if min(width, height) < 1:
        size /= min(width, height)
    if size.width() * size.height() > 40_000_000:
        raise MindMapError("SVG 图像尺寸超过四千万逻辑像素")
    return renderer, size
