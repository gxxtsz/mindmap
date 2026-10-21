"""亿图普通脑图的 XML 剪贴板协议；只提取文字和有序树，不读取外部文件。"""

from html.parser import HTMLParser
from xml.etree import ElementTree

from mindmap_core import MindMapDocument, MindMapError, Node

from .limits import MAX_ARCHIVE_BYTES

# 这些类型表示真正的主题；连接线、概要及其标题、图片和任意绘图对象均不导入。
_TOPIC_TYPES = frozenset({"MainIdea", "MainTopic", "SubTopic", "Floating", "Topic"})


class _ClipboardTreeBuilder(ElementTree.TreeBuilder):
    """剪贴板只需要普通 XML 元素，拒绝 DTD，避免展开输入中声明的实体。"""

    def doctype(self, name, pubid, system):
        raise MindMapError("亿图剪贴板不支持文档类型或实体声明")


class _PlainHtml(HTMLParser):
    """将亿图 HtmlText 转为纯文字，保留段落和显式换行，丢弃样式及脚本。"""

    _BLOCKS = frozenset({"p", "div", "li", "tr", "blockquote", "pre", *[f"h{i}" for i in range(1, 7)]})
    _IGNORED = frozenset({"head", "style", "script"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored = 0
        self.trailing_block_break = False

    def _break(self, *, explicit=False):
        if explicit or (self.parts and not self.parts[-1].endswith("\n")):
            self.parts.append("\n")
            self.trailing_block_break = not explicit

    def handle_starttag(self, tag, attrs):
        if tag in self._IGNORED:
            self.ignored += 1
        if self.ignored:
            return
        if tag == "br":
            self._break(explicit=True)
        elif tag in self._BLOCKS:
            self._break()
        elif tag == "td" and self.parts and not self.parts[-1].endswith(("\n", "\t")):
            self.parts.append("\t")

    def handle_endtag(self, tag):
        if tag in self._IGNORED:
            self.ignored = max(0, self.ignored - 1)
        elif not self.ignored and tag in self._BLOCKS:
            self._break()

    def handle_data(self, data):
        if self.ignored or (not data.strip() and any(char in data for char in "\r\n")):
            return
        if data:
            self.parts.append(data)
            self.trailing_block_break = False

    def plain_text(self):
        """仅移除 HTML 块结尾产生的一个分隔符，不剥除正文首尾空格或显式换行。"""
        parts = self.parts[:-1] if self.trailing_block_break else self.parts
        return "".join(parts).replace("\xa0", " ")


def _topic_text(shape):
    """兼容亿图结构化的 pp/tp 段落与复制专用的 HtmlText，两者均输出纯文本。"""
    html = shape.find("./Text/HtmlText")
    if html is not None:
        parser = _PlainHtml()
        parser.feed("".join(html.itertext()))
        parser.close()
        text = parser.plain_text()
    else:
        content = shape.find("./Text/TextBlock/Text")
        if content is None:
            return ""
        paragraphs = content.findall("pp")
        # 只连接正文 run，避免把 XML 美化缩进当成用户输入的空格和换行。
        text = "\n".join(
            "".join("".join(run.itertext()) for run in paragraph.findall("tp"))
            for paragraph in paragraphs
        )
        if not paragraphs:
            text = "".join(content.itertext())
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\u2028", "\n").replace("\u2029", "\n")


def _references(shape, name):
    """解析 LevelData 的分号分隔标识；0 表示未引用任何来源节点。"""
    element = shape.find(f"./LevelData/{name}")
    value = element.get("V", "") if element is not None else ""
    return [key for item in value.split(";") if (key := item.strip()) and key != "0"]


def import_edraw_xml(data: bytes) -> MindMapDocument:
    """把亿图 XML 剪贴板解析为带合成根的独立片段，失败时抛出中文 MindMapError。

    输入必须是 XML 字节，文件路径载荷由 Qt 适配层限量读取后再传入。来源 ID 仅用于
    还原层级，不进入结果；所有真实节点均建立新标识，且只有正文和 children 被赋值。
    合成根的 children 支持多棵并列来源树，避免将第二个主题误挂到第一个主题下面。
    本接口不注册为文件格式，也不依赖 Qt、布局实现或亿图的安装位置。
    """
    if not isinstance(data, bytes):
        raise MindMapError("亿图剪贴板内容必须是 XML 字节")
    if len(data) > MAX_ARCHIVE_BYTES:
        raise MindMapError("亿图剪贴板大小超过读取限制")
    try:
        root = ElementTree.fromstring(data, parser=ElementTree.XMLParser(target=_ClipboardTreeBuilder()))
    except (ElementTree.ParseError, ValueError, LookupError) as exc:
        raise MindMapError("亿图剪贴板 XML 损坏，请在亿图中重新复制") from exc
    if root.tag != "Clipboard" or root.find("Shapes") is None:
        raise MindMapError("亿图剪贴板缺少 Clipboard/Shapes 结构")

    shapes, nodes = {}, {}
    for shape in root.findall("./Shapes/Shape"):
        if shape.get("Type") not in _TOPIC_TYPES:
            continue
        key = shape.get("ID", "").strip()
        if not key or key == "0":
            raise MindMapError("亿图主题缺少有效的节点 ID")
        if key in shapes:
            raise MindMapError(f"亿图剪贴板包含重复节点 ID：{key}")
        shapes[key], nodes[key] = shape, Node(_topic_text(shape))
    if not nodes:
        raise MindMapError("亿图剪贴板未包含可粘贴的主题")

    parents = {}
    children = {key: [] for key in nodes}

    def connect(parent, child):
        """只连接复制范围内的主题；重复边、多父节点和冲突引用均拒绝，不猜测层级。"""
        if child in parents and parents[child] != parent:
            raise MindMapError("亿图剪贴板层级包含重复父节点")
        if child == parent:
            raise MindMapError("亿图剪贴板层级包含循环")
        parents[child] = parent
        children[parent].append(child)

    # SubLevel 是同级顺序的来源。范围外的引用可能属于未复制的分支或装饰对象，直接忽略。
    for parent, shape in shapes.items():
        seen = set()
        for child in _references(shape, "SubLevel"):
            if child not in nodes:
                continue
            if child in seen:
                raise MindMapError("亿图剪贴板层级包含重复子节点")
            seen.add(child)
            connect(parent, child)

    # Super 补齐只有父引用的数据；父节点缺席时，该节点自然成为森林的一棵根。
    # 此处不递归，处理深树时不受 Python 调用栈深度影响。
    for child, shape in shapes.items():
        references = _references(shape, "Super")
        if len(references) > 1:
            raise MindMapError("亿图主题包含多个父节点引用")
        parent = references[0] if references else None
        if parent in nodes and parents.get(child) != parent:
            connect(parent, child)

    document = MindMapDocument.new("亿图剪贴板")
    # 此根节点只是剪贴板森林的临时容器，不是新建导图；粘贴外观由目标主题决定。
    document.root.style.clear()
    document.root.children = [node.id for key, node in nodes.items() if key not in parents]
    for key, node in nodes.items():
        node.children = [nodes[child].id for child in children[key]]
        document.nodes[node.id] = node
    try:
        document.validate()
    except MindMapError as exc:
        raise MindMapError(f"亿图剪贴板层级无效，可能包含循环：{exc}") from exc
    return document
