"""内置格式的装配工厂；每次调用都创建独立的注册表与编解码器。"""

from mindmap_core import FormatInfo, FormatRegistry, MindMapError

from .json_codec import JsonCodec
from .limits import MAX_ARCHIVE_BYTES
from .mindmap import MindmapCodec
from .outline import export_outline, import_indented_fragment, import_outline


class OutlineCodec:
    """将公共大纲转换函数适配为二进制流接口。"""

    def __init__(self, *, markdown: bool):
        self.markdown = markdown

    def read(self, stream, *, options):
        """限量读取 UTF-8 大纲，返回独立的新文档或纯文本多根片段。

        stream 为调用方提供的二进制流，本方法只读取，不关闭或保存它。options 中
        as_fragment 是纯文本导入专用布尔选项，默认 False，维持首行为根的单根约定；
        True 时自动识别 Tab、2／4 空格，返回可直接粘贴的带合成根片段。Markdown
        继续沿用原有导入语义。超限、非法选项及解析失败抛出中文 MindMapError，
        缩进／层级错误包含原文行号；不修改宿主文档、文件或撤销历史。
        """
        content = stream.read(MAX_ARCHIVE_BYTES + 1)
        if len(content) > MAX_ARCHIVE_BYTES:
            raise MindMapError("文档大小超过读取限制")
        text = content.decode("utf-8-sig")
        if not self.markdown:
            as_fragment = options.get("as_fragment", False)
            if not isinstance(as_fragment, bool):
                raise MindMapError("文本导入选项 as_fragment 必须为布尔值")
            if as_fragment:
                return import_indented_fragment(text)
        return import_outline(text, markdown=self.markdown)

    def write(self, document, stream, *, options):
        """向调用方提供的二进制流写入 UTF-8 大纲。

        纯文本格式接受布尔选项 tab_indent，默认使用两空格缩进，设为 True 时
        启用当前页 TXT 格式。Markdown 由构造选项决定。文件提交由调用方负责。
        """
        tab_indent = not self.markdown and (options or {}).get("tab_indent", False)
        if not isinstance(tab_indent, bool):
            raise MindMapError("文本导出选项 tab_indent 必须为布尔值")
        stream.write(export_outline(document, markdown=self.markdown, tab_indent=tab_indent).encode("utf-8"))


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
