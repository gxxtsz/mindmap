"""亿图剪贴板的系统适配；文件读取限量进行，格式解析仍由无 Qt 的引擎包负责。"""

from pathlib import Path

from mindmap_core import MindMapDocument, MindMapError
from mindmap_formats import MAX_ARCHIVE_BYTES, import_edraw_xml

EDRAW_CLIPBOARD_MIME = "application/edraw-mind-xml"


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
