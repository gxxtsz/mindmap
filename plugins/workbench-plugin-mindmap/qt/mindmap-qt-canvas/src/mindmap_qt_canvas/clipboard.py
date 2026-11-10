"""节点片段及图片的剪贴板适配；图片同步读取，亿图结构解析交给无 Qt 引擎包。"""

from pathlib import Path

from mindmap_core import MindMapDocument, MindMapError
from mindmap_formats import MAX_ARCHIVE_BYTES, import_edraw_xml
from mindmap_qt_render.rendering import decode_image
from PySide6.QtCore import QBuffer, QIODevice, QMimeData
from PySide6.QtGui import QImage, QImageReader, QPixmap

EDRAW_CLIPBOARD_MIME = "application/edraw-mind-xml"


def _clipboard_image_files(mime: QMimeData) -> list[Path]:
    """查询文件复制载荷中的本地图片路径，不读盘、不接管纯文字中的路径。

    仅接受全部条目均为有效、绝对的本地文件 URL，且扩展名受当前 Qt 图像插件支持
    的载荷；混入网址、普通文件或相对路径时返回空列表，保留原有粘贴行为。识别时
    不检查文件是否存在，使已删除、不可读或损坏的图片在读取阶段报错，而非粘成路径。
    返回独立的 Path 列表，不保留借用的 MIME 对象；中文、空格及转义字符由 QUrl 解码。
    """
    if not mime.hasUrls():
        return []
    extensions = {bytes(kind).decode("ascii").lower() for kind in QImageReader.supportedImageFormats()}
    paths = []
    for address in mime.urls():
        if not address.isValid() or not address.isLocalFile():
            return []
        path = Path(address.toLocalFile())
        if not path.is_absolute() or path.suffix.lstrip(".").lower() not in extensions:
            return []
        paths.append(path)
    return paths


def has_clipboard_image(mime: QMimeData) -> bool:
    """查询借用的 MIME 载荷是否声明图像，返回布尔值，不解码或修改剪贴板。

    mime 是当前 GUI 线程读取到的 QMimeData；调用期间必须有效，本函数不保留引用。
    同时检查 Qt 位图、image/* 格式及本地图片文件 URL，兼容独立 PNG 和资源管理器
    复制图片时 hasImage() 为 False 的载荷。纯文字路径、网址和 HTML 不触发文件读取。
    声明图像不代表内容有效，实际读取由 read_clipboard_image 校验；正常调用无异常。
    """
    return (mime.hasImage() or any(kind.startswith("image/") for kind in mime.formats())
            or bool(_clipboard_image_files(mime)))


def read_clipboard_image(mime: QMimeData) -> tuple[bytes, str] | None:
    """同步读取剪贴板图像，返回独立的图片字节与实际 MIME，无图像时返回 None。

    mime 是 GUI 线程内短暂借用的 QMimeData，不转移所有权，也不缓存到调用结束之后。
    优先使用可解码的编码图片，保留原始字节；仅有 QImage、QPixmap 时无损编码为 PNG，
    保留透明通道；其次读取明确复制的单张本地图片文件，按实际内容识别 MIME 并保留
    原始字节。多个图像表示可相互回退，但全部无效时抛出中文 MindMapError，
    调用方不能再降级为文本。空图、超过四千万像素或 PNG 编码失败同样抛出该异常。
    本地文件失效、无法读取、超过 MAX_ARCHIVE_BYTES 或一次复制多张图片时均抛出中文
    MindMapError，不悄悄忽略其他文件或降级为路径文字。纯文字路径、网址和 HTML 引用
    不读取；不修改文档、草稿、历史及系统剪贴板，文件句柄在返回或异常前关闭。
    """
    if not has_clipboard_image(mime):
        return None
    kinds = [kind for kind in mime.formats() if kind.startswith("image/")]
    # PNG 表示通常保留截图透明度；其余编码按来源声明顺序尝试。
    kinds.sort(key=lambda kind: kind != "image/png")
    error = None
    for kind in kinds:
        try:
            data = bytes(mime.data(kind))
            _, media_type = decode_image(data)
            return data, media_type
        except MindMapError as exc:
            # 只保存消息，异常回溯会反向持有本次帧及借用的系统 MIME 对象。
            error = str(exc)
    if mime.hasImage():
        value = mime.imageData()
        if isinstance(value, (QImage, QPixmap)) and not value.isNull():
            if value.width() * value.height() > 40_000_000:
                raise MindMapError("图片无效或像素数超过四千万")
            image = value.toImage() if isinstance(value, QPixmap) else value
            buffer = QBuffer()
            if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not image.save(buffer, "PNG"):
                raise MindMapError("剪贴板图片无法编码为 PNG")
            return bytes(buffer.data()), "image/png"
    paths = _clipboard_image_files(mime)
    if paths:
        if len(paths) != 1:
            raise MindMapError("一次只能粘贴一张本地图片，请选择一张图片重新复制")
        path = paths[0]
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_ARCHIVE_BYTES + 1)
        except OSError as exc:
            raise MindMapError(f"剪贴板图片文件已失效或无法读取：{path.name}") from exc
        if len(data) > MAX_ARCHIVE_BYTES:
            raise MindMapError("剪贴板图片文件大小超过读取限制")
        _, media_type = decode_image(data)
        return data, media_type
    if error is not None:
        raise MindMapError(error)
    raise MindMapError("剪贴板图片无效或格式不受支持")


def read_edraw_clipboard(data: bytes) -> MindMapDocument:
    """解析专用 MIME 的 XML 或本机绝对 copy.xml 路径，不解释普通文本剪贴板中的路径。

    亿图大批量复制时只在 MIME 中写入临时文件的 UTF-8 路径；文件可能在再次复制或
    退出亿图后失效。读取上限与其他文档格式一致，任何读取或解析错误都交给调用者，
    禁止回退到纯文本而悄悄丢失树结构。UTF-16 等带 XML 编码声明的正文由 XML 解析器处理。
    """
    if len(data) > MAX_ARCHIVE_BYTES:
        raise MindMapError("亿图剪贴板大小超过读取限制")
    try:
        text = data.decode("utf-8-sig").rstrip("\0").strip()
    except UnicodeDecodeError:
        return import_edraw_xml(data)
    if "\0" in text:
        return import_edraw_xml(data)
    if text.startswith("<"):
        return import_edraw_xml(data.rstrip(b"\0"))
    try:
        path = Path(text)
        if not text or "\0" in text or not path.is_absolute() or path.name.lower() != "copy.xml":
            raise MindMapError("亿图剪贴板既不是 XML，也不是有效的本机 copy.xml 路径")
        with path.open("rb") as stream:
            content = stream.read(MAX_ARCHIVE_BYTES + 1)
    except OSError as exc:
        raise MindMapError("亿图剪贴板文件已失效或无法读取，请在亿图中重新复制") from exc
    if len(content) > MAX_ARCHIVE_BYTES:
        raise MindMapError("亿图剪贴板大小超过读取限制")
    return import_edraw_xml(content)
