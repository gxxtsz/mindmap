"""节点图的有向关系和持久状态，供引擎、文本编解码和布局共用。"""

import re
from dataclasses import asdict, dataclass, field, replace

from .attachments import FileAttachment
from .errors import MindMapError
from .geometry import Point

GRAPH_NODE_ID = r"[A-Za-z_][A-Za-z0-9_-]*"
GRAPH_ATTACHMENT_VERSION = 4
# 滚动条的原生范围为有符号整数，限制持久坐标以预留 400% 缩放和完整视口余量。
_MAX_GRAPH_COORDINATE = 100_000_000


@dataclass(frozen=True, slots=True)
class GraphNode:
    """图内节点。局部 ID 用于连线和图文本，允许在不同的图中重复。

    document_node_id 在首次创建附页时由引擎分配，指向文档节点表中承载附页的实体。
    普通图节点为 None；调用方可读取绑定，但只能通过引擎的页面操作建立绑定。
    title 保存原始正文，text_format 指定显示为纯文本（plain）或 Markdown（markdown）。
    fixed_width 是不含留白及附页按钮的固定正文宽度，单位为逻辑像素，范围为 40 至 1200。
    None 表示按内容自动测量。构造和旧图解码默认使用纯文本与自动宽度。
    attachment 保存关联文件的名称及文档内资源标识。
    """

    id: str
    title: str
    document_node_id: str | None = None
    text_format: str = "plain"
    fixed_width: float | None = None
    attachment: FileAttachment | None = None


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """从 source 指向 target 的有向连线，允许自环及反向连线。"""

    source: str
    target: str

    @property
    def key(self) -> str:
        """产生布局图元的稳定键；节点标识语法排除了箭头中的大于号。"""
        return f"{self.source}->{self.target}"


@dataclass(frozen=True, slots=True)
class NodeGraph:
    """一张图的内容快照；节点和连线保留声明顺序，坐标使用逻辑像素左上角。

    positions 只包含手动移动过的节点；每条有向边都参与布局与绘制。
    引擎读写都复制位置字典，返回的快照可独立使用；界面读模型中的位置字典只读借用。
    """

    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[GraphEdge, ...] = ()
    positions: dict[str, Point] = field(default_factory=dict)

    def with_content(self, nodes: tuple[GraphNode, ...], edges: tuple[GraphEdge, ...]) -> "NodeGraph":
        """替换节点和连线，按仍存在的局部 ID 保留手动位置、绑定及附件。

        格式和固定宽度取自 nodes。已有节点的附页绑定从当前图继承。删除节点时
        一并移除相应绑定，附页实体由引擎提交时级联删除。
        """
        ids = {node.id for node in nodes}
        previous = {node.id: node for node in self.nodes if node.document_node_id or node.attachment}
        nodes = tuple(
            replace(node, document_node_id=old.document_node_id or node.document_node_id,
                    attachment=node.attachment or old.attachment)
            if (old := previous.get(node.id)) is not None else node
            for node in nodes
        )
        return replace(self, nodes=nodes, edges=edges,
                       positions={key: point for key, point in self.positions.items() if key in ids})

    def to_data(self) -> dict:
        """编码为独立 JSON 对象，省略默认值及空附件。

        旧筛选字段写为空数组，节点顺序和手动位置随记录保存。字段异常时抛出
        MindMapError。调用方使用 to_record() 获取与这些字段匹配的版本。
        """
        try:
            nodes = []
            for node in self.nodes:
                record = {"id": node.id, "title": node.title}
                if node.document_node_id is not None:
                    record["document_node_id"] = node.document_node_id
                if node.text_format != "plain":
                    record["text_format"] = node.text_format
                if node.fixed_width is not None:
                    record["fixed_width"] = node.fixed_width
                if node.attachment is not None:
                    record["attachment"] = asdict(node.attachment)
                nodes.append(record)
            return {
                "nodes": nodes,
                "edges": [[edge.source, edge.target] for edge in self.edges],
                "positions": {key: list(point) for key, point in self.positions.items()},
                "hidden_node_ids": [],
            }
        except (AttributeError, TypeError) as error:
            raise MindMapError("图的数据字段无效") from error

    def to_record(self) -> dict:
        """为原生图写入满足全部节点字段的最低版本，并返回独立记录。

        附件需要版本四，Markdown 或固定宽度需要版本三，附页绑定需要版本二。
        """
        data = self.to_data()
        if any("attachment" in node for node in data["nodes"]):
            version = GRAPH_ATTACHMENT_VERSION
        elif any("text_format" in node or "fixed_width" in node for node in data["nodes"]):
            version = 3
        else:
            version = 2 if any("document_node_id" in node for node in data["nodes"]) else 1
        return {"version": version, "data": data}

    @classmethod
    def from_data(cls, data: dict, *, version: int = GRAPH_ATTACHMENT_VERSION) -> "NodeGraph":
        """解码原生节点页，校验局部标识、端点及坐标，失败时抛出中文错误。

        version 取自原生图封装。版本二增加可选的文档节点绑定，版本三增加可选的
        正文格式和固定宽度，版本四增加附件引用。绑定在本图内唯一，存在性和全局
        所有权由完整文档校验。旧筛选字段只读取和校验。
        """
        from .validation import finite, validate_fixed_width

        if type(version) is not int or not 1 <= version <= GRAPH_ATTACHMENT_VERSION:
            raise MindMapError("不支持的图数据版本")
        if (not isinstance(data, dict) or set(data) != {"nodes", "edges", "positions", "hidden_node_ids"}
                or not all(isinstance(data[key], list) for key in ("nodes", "edges", "hidden_node_ids"))
                or not isinstance(data["positions"], dict)):
            raise MindMapError("图的数据字段无效")
        nodes = []
        ids = set()
        document_ids = set()
        required = {"id", "title"}
        allowed = required.copy()
        if version >= 2:
            allowed.add("document_node_id")
        if version >= 3:
            allowed.update(("text_format", "fixed_width"))
        if version >= GRAPH_ATTACHMENT_VERSION:
            allowed.add("attachment")
        for record in data["nodes"]:
            if not isinstance(record, dict) or not required <= record.keys() <= allowed:
                raise MindMapError("图节点必须包含标识和标题")
            key, title = record["id"], record["title"]
            if not isinstance(key, str) or not re.fullmatch(GRAPH_NODE_ID, key) or key in ids:
                raise MindMapError(f"图节点标识无效或重复：{key!r}")
            validate_graph_title(title)
            ids.add(key)
            document_id = record.get("document_node_id")
            if "document_node_id" in record:
                if not isinstance(document_id, str) or not document_id or document_id in document_ids:
                    raise MindMapError("图节点的文档标识无效或重复")
                document_ids.add(document_id)
            text_format = record.get("text_format", "plain")
            if text_format not in ("plain", "markdown"):
                raise MindMapError("图节点文本格式必须是 plain 或 markdown")
            fixed_width = record.get("fixed_width")
            if "fixed_width" in record:
                validate_fixed_width(fixed_width)
            attachment = FileAttachment.from_data(record["attachment"]) if "attachment" in record else None
            nodes.append(GraphNode(key, title, document_id, text_format, fixed_width, attachment))
        edges = {}
        for pair in data["edges"]:
            if (not isinstance(pair, list) or len(pair) != 2
                    or not all(isinstance(key, str) and key in ids for key in pair)):
                raise MindMapError(f"图连线引用了无效的节点：{pair!r}")
            edge = GraphEdge(*pair)
            if edge in edges:
                raise MindMapError(f"图连线重复：{edge.key}")
            edges[edge] = None
        positions = {}
        for key, point in data["positions"].items():
            if (key not in ids or not isinstance(point, list) or len(point) != 2
                    or not all(finite(value) and abs(value) <= _MAX_GRAPH_COORDINATE for value in point)):
                raise MindMapError(f"图节点坐标无效：{key!r}")
            positions[key] = tuple(map(float, point))
        hidden = data["hidden_node_ids"]
        if (not all(isinstance(key, str) and key in ids for key in hidden) or len(set(hidden)) != len(hidden)):
            raise MindMapError("图的筛选包含重复或未声明的节点")
        return cls(tuple(nodes), tuple(edges), positions)


def validate_graph_title(title: object) -> None:
    """校验标题可进入 UTF-8 原生文档；空正文与普通导图节点一样有效。"""
    if not isinstance(title, str):
        raise MindMapError("图节点标题必须是双引号字符串")
    try:
        title.encode("utf-8")
    except UnicodeError as error:
        raise MindMapError("图节点标题包含无效的 Unicode 字符") from error
