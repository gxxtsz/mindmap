"""内置格式的装配工厂；每次调用都创建独立的注册表与编解码器。"""

from mindmap_core import FormatInfo, FormatRegistry, MindMapError

from .json_codec import JsonCodec
from .limits import MAX_ARCHIVE_BYTES
from .mindmap import MindmapCodec
from .outline import export_outline, import_outline


class OutlineCodec:
    """将公共大纲转换函数适配为二进制流接口。"""

    def __init__(self, *, markdown: bool):
        self.markdown = markdown

    def read(self, stream, *, options):
        """大纲导入会创建新节点标识，样式和附件不属于此交换格式。"""
        content = stream.read(MAX_ARCHIVE_BYTES + 1)
        if len(content) > MAX_ARCHIVE_BYTES:
            raise MindMapError("文档大小超过读取限制")
        return import_outline(content.decode("utf-8-sig"), markdown=self.markdown)

    def write(self, document, stream, *, options):
        """只输出层级与文字，不建立保存标记。"""
        stream.write(export_outline(document, markdown=self.markdown).encode("utf-8"))


def create_default_format_registry() -> FormatRegistry:
    """装配原生容器、JSON、Markdown 与纯文本，不加载 Qt 或其他扩展包。"""
    registry = FormatRegistry()
    entries = (
        (FormatInfo("mindmap", "思维导图", (".mindmap",), True), MindmapCodec()),
        (FormatInfo("json", "JSON 文档", (".json",), True), JsonCodec()),
        (FormatInfo("markdown", "Markdown 大纲", (".md",)), OutlineCodec(markdown=True)),
        (FormatInfo("text", "缩进文本", (".txt",)), OutlineCodec(markdown=False)),
    )
    for info, codec in entries:
        registry.register(info, importer=codec, exporter=codec)
    return registry
