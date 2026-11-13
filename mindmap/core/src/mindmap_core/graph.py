"""节点图的有向关系和持久状态，供引擎、文本编解码和布局共用。"""

import re
from dataclasses import dataclass, field, replace

from .errors import MindMapError
from .geometry import Point

GRAPH_NODE_ID = r"[A-Za-z_][A-Za-z0-9_-]*"
# 滚动条的原生范围为有符号整数，限制持久坐标以预留 400% 缩放和完整视口余量。
_MAX_GRAPH_COORDINATE = 100_000_000


@dataclass(frozen=True, slots=True)
class GraphNode:
    """图内节点。局部 ID 用于连线和图文本，允许在不同的图中重复。

    document_node_id 在首次创建附页时由引擎分配，指向文档节点表中承载附页的实体。
    普通图节点为 None；调用方可读取绑定，但只能通过引擎的页面操作建立绑定。
    """

    id: str
    title: str
    document_node_id: str | None = None


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
        """替换节点和连线，按仍存在的局部 ID 保留手动位置及文档节点绑定。

        图文本只提供局部标识和标题。已有节点的附页绑定从当前图继承；删除节点后
        相应绑定从结果移除，实际级联删除由引擎提交时处理。
        """
        ids = {node.id for node in nodes}
        bindings = {node.id: node.document_node_id for node in self.nodes if node.document_node_id}
        nodes = tuple(replace(node, document_node_id=bindings[node.id]) if node.id in bindings else node
                      for node in nodes)
        return replace(self, nodes=nodes, edges=edges,
                       positions={key: point for key, point in self.positions.items() if key in ids})

    def to_data(self) -> dict:
        """编码为独立的 JSON 对象。文档节点绑定只在存在时写出，旧筛选字段写为空数组。"""
        try:
            return {
                "nodes": [
                    {"id": node.id, "title": node.title, **(
                        {"document_node_id": node.document_node_id} if node.document_node_id is not None else {}
                    )} for node in self.nodes
                ],
                "edges": [[edge.source, edge.target] for edge in self.edges],
                "positions": {key: list(point) for key, point in self.positions.items()},
                "hidden_node_ids": [],
            }
        except (AttributeError, TypeError) as error:
            raise MindMapError("图的数据字段无效") from error

    def to_record(self) -> dict:
        """封装原生图数据。有文档节点绑定时写版本二，否则继续写版本一。"""
        data = self.to_data()
        return {"version": 2 if any("document_node_id" in node for node in data["nodes"]) else 1, "data": data}

    @classmethod
    def from_data(cls, data: dict, *, version: int = 2) -> "NodeGraph":
        """解码原生节点页，校验局部标识、端点及坐标，失败时抛出中文错误。

        version 取自原生图封装；版本一仅接受原有节点字段，版本二允许可选的文档节点绑定。
        绑定在本图内唯一，存在性和全局所有权由完整文档校验。旧筛选字段只读取和校验。
        """
        from .validation import finite

        if type(version) is not int or version not in (1, 2):
            raise MindMapError("不支持的图数据版本")
        if (not isinstance(data, dict) or set(data) != {"nodes", "edges", "positions", "hidden_node_ids"}
                or not all(isinstance(data[key], list) for key in ("nodes", "edges", "hidden_node_ids"))
                or not isinstance(data["positions"], dict)):
            raise MindMapError("图的数据字段无效")
        nodes = []
        ids = set()
        document_ids = set()
        for record in data["nodes"]:
            if not isinstance(record, dict) or (set(record) != {"id", "title"}
                    and (version != 2 or set(record) != {"id", "title", "document_node_id"})):
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
            nodes.append(GraphNode(key, title, document_id))
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
