"""节点图的有向关系和持久状态，供引擎、文本编解码和布局共用。"""

import re
from collections.abc import Collection, Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field, replace
from itertools import count

from .attachments import FileAttachment
from .errors import MindMapError
from .geometry import Point

GRAPH_NODE_ID = r"[A-Za-z_][A-Za-z0-9_-]*"
GRAPH_ATTACHMENT_VERSION = 4
GRAPH_IMAGE_VERSION = 5
LEGACY_GRAPH_GROUP_VERSION = 6
GRAPH_GROUP_VERSION = 7
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
    attachment 保存关联文件的名称及文档内资源标识。纯图片节点通过 image_id
    引用文档内 SVG，image_width 保存其逻辑显示宽度。
    """

    id: str
    title: str
    document_node_id: str | None = None
    text_format: str = "plain"
    fixed_width: float | None = None
    attachment: FileAttachment | None = None
    image_id: str | None = None
    image_width: float | None = None

    def asset_ids(self):
        """遍历图节点直接引用的文档资源。"""
        if self.image_id is not None:
            yield self.image_id
        if self.attachment is not None:
            yield self.attachment.asset_id


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
class GraphGroup:
    """图节点的分组属性，成员按声明顺序保存。

    id 引用同图中的文字节点，名称、正文及附页由该节点提供。member_ids 保存直接
    成员，允许为空或包含其他组节点。每个成员只有一个直接父组，父组关系不能成环。
    min_size 是手动设定的最小宽高，使用场景逻辑像素。None 表示按组内内容测量。
    """

    id: str
    member_ids: tuple[str, ...] = ()
    min_size: Point | None = None

    def remap(self, node_ids: Mapping[str, str]) -> "GraphGroup":
        """按同一份节点粘贴映射重建组和成员，返回保留手动宽高的独立分组。"""
        return replace(self, id=node_ids[self.id], member_ids=tuple(node_ids[key] for key in self.member_ids))


@dataclass(frozen=True, slots=True)
class NodeGraph:
    """一张图的内容快照；节点和连线保留声明顺序，坐标使用逻辑像素左上角。

    positions 记录需要保留的节点位置；每条有向边都参与布局与绘制。
    groups 默认为空。组节点和成员共用局部节点标识，有向边只连接直接父组相同的节点。
    引擎读写都复制位置字典，返回的快照可独立使用；界面读模型中的位置字典只读借用。
    """

    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[GraphEdge, ...] = ()
    positions: dict[str, Point] = field(default_factory=dict)
    groups: tuple[GraphGroup, ...] = ()

    def with_content(self, nodes: tuple[GraphNode, ...], edges: tuple[GraphEdge, ...]) -> "NodeGraph":
        """替换节点和连线，按仍存在的局部 ID 保留手动位置、绑定、附件和分组。

        格式和固定宽度取自 nodes。已有节点的附页绑定从当前图继承。删除节点时
        一并移除相应绑定及分组引用，附页实体由引擎提交时级联删除。空组仍由组节点承载。
        """
        ids = {node.id for node in nodes}
        previous = {node.id: node for node in self.nodes if node.document_node_id or node.attachment}
        nodes = tuple(
            replace(node, document_node_id=old.document_node_id or node.document_node_id,
                    attachment=node.attachment or old.attachment)
            if (old := previous.get(node.id)) is not None else node
            for node in nodes
        )
        groups = tuple(replace(group, member_ids=tuple(key for key in group.member_ids if key in ids))
                       for group in self.groups if group.id in ids)
        return replace(self, nodes=nodes, edges=edges, groups=groups,
                       positions={key: point for key, point in self.positions.items() if key in ids})

    def parent_groups(self) -> dict[str, str]:
        """返回直接成员到父组的映射。顶层节点没有条目，结果可由调用方独立使用。"""
        return {key: group.id for group in self.groups for key in group.member_ids}

    def subtree_ids(self, roots: Iterable[str]) -> set[str]:
        """返回指定节点及其分组后代的局部标识集合，重复选择同一分支只计一次。"""
        members = {group.id: group.member_ids for group in self.groups}
        pending, seen = list(roots), set()
        while pending:
            key = pending.pop()
            if key not in seen:
                seen.add(key)
                pending.extend(members.get(key, ()))
        return seen

    def with_group_parents(self, destinations: Mapping[str, str | None]) -> "NodeGraph":
        """调整现有节点的直接归属并转接越层连线，返回待提交的图。

        destinations 的值为目标组节点标识，None 表示顶层。未涉及的成员顺序和宽高
        保留，新成员按输入顺序追加。非法目标、循环或无法提升到同层的线抛出 MindMapError。
        该方法无副作用，调用方可把新坐标与结果一并提交到引擎。
        """
        node_ids = {node.id for node in self.nodes}
        group_ids = {group.id for group in self.groups}
        parents = self.parent_groups()
        moved = {key: parent for key, parent in destinations.items() if parents.get(key) != parent}
        if any(key not in node_ids or parent is not None and parent not in group_ids
               for key, parent in destinations.items()):
            raise MindMapError("待移动节点或目标分组已失效")
        if not moved:
            return self
        incoming: dict[str, list[str]] = {}
        for key, parent in moved.items():
            if parent is None:
                parents.pop(key, None)
            else:
                parents[key] = parent
                incoming.setdefault(parent, []).append(key)
        if graph_membership_cycle(parents) is not None:
            raise MindMapError("分组不能放入自身或后代")
        groups = tuple(replace(group, member_ids=(
            *(key for key in group.member_ids if key not in moved), *incoming.get(group.id, ()),
        )) for group in self.groups)
        return replace(self, groups=groups, edges=promote_graph_edges(self.edges, parents))

    def to_data(self) -> dict:
        """编码为独立 JSON 对象，省略默认值、空附件和空分组表。

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
                if node.image_id is not None:
                    record["image_id"] = node.image_id
                if node.image_width is not None:
                    record["image_width"] = node.image_width
                nodes.append(record)
            data = {
                "nodes": nodes,
                "edges": [[edge.source, edge.target] for edge in self.edges],
                "positions": {key: list(point) for key, point in self.positions.items()},
                "hidden_node_ids": [],
            }
            if not isinstance(self.groups, tuple):
                raise MindMapError("图分组必须以元组提供")
            if self.groups:
                records = []
                for group in self.groups:
                    if not isinstance(group, GraphGroup) or not isinstance(group.member_ids, tuple):
                        raise MindMapError("图分组及其成员类型无效")
                    record = {"id": group.id, "member_ids": list(group.member_ids)}
                    if group.min_size is not None:
                        record["min_size"] = list(group.min_size)
                    records.append(record)
                data["groups"] = records
            return data
        except (AttributeError, TypeError) as error:
            raise MindMapError("图的数据字段无效") from error

    def to_record(self) -> dict:
        """为原生图写入满足全部字段的最低版本，并返回独立记录。

        可连线的分组需要版本七，图片节点需要版本五，附件需要版本四，
        Markdown 或固定宽度需要版本三，附页绑定需要版本二。
        """
        data = self.to_data()
        if self.groups:
            version = GRAPH_GROUP_VERSION
        elif any("image_id" in node or "image_width" in node for node in data["nodes"]):
            version = GRAPH_IMAGE_VERSION
        elif any("attachment" in node for node in data["nodes"]):
            version = GRAPH_ATTACHMENT_VERSION
        elif any("text_format" in node or "fixed_width" in node for node in data["nodes"]):
            version = 3
        else:
            version = 2 if any("document_node_id" in node for node in data["nodes"]) else 1
        return {"version": version, "data": data}

    @classmethod
    def from_data(cls, data: dict, *, version: int = GRAPH_GROUP_VERSION) -> "NodeGraph":
        """解码原生节点页，校验局部标识、端点、坐标及分组，失败时抛出中文错误。

        version 取自原生图封装。版本二增加可选的文档节点绑定，版本三增加可选的
        正文格式和固定宽度，版本四增加附件引用，版本五增加纯图片节点，
        版本六的分组在读取时升级为版本七的图节点，并把已有越层线转接到组节点。
        缺省分组表为空。绑定在本图内唯一，存在性和全局所有权由完整文档校验。
        旧筛选字段只读取和校验。
        """
        from .validation import finite, positive_number, validate_fixed_width

        if type(version) is not int or not 1 <= version <= GRAPH_GROUP_VERSION:
            raise MindMapError("不支持的图数据版本")
        fields = {"nodes", "edges", "positions", "hidden_node_ids"}
        allowed_fields = fields | {"groups"} if version >= LEGACY_GRAPH_GROUP_VERSION else fields
        if (not isinstance(data, dict) or not fields <= data.keys() <= allowed_fields
                or not all(isinstance(data[key], list) for key in ("nodes", "edges", "hidden_node_ids"))
                or not isinstance(data.get("groups", []), list)
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
        if version >= GRAPH_IMAGE_VERSION:
            allowed.update(("image_id", "image_width"))
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
            image_id, image_width = record.get("image_id"), record.get("image_width")
            if "image_id" in record or "image_width" in record:
                if not isinstance(image_id, str) or not re.fullmatch(r"[a-f0-9]{64}", image_id):
                    raise MindMapError("图节点的图片资源标识无效")
                positive_number(image_width, "图片宽度")
                if title or document_id is not None or fixed_width is not None or text_format != "plain":
                    raise MindMapError("纯图片图节点只能保存图片内容与尺寸")
            nodes.append(GraphNode(key, title, document_id, text_format, fixed_width, attachment,
                                   image_id, image_width))
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
        graph = cls(tuple(nodes), tuple(edges), positions)
        if version == LEGACY_GRAPH_GROUP_VERSION:
            legacy = []
            group_ids, grouped_ids = set(), set()
            for record in data.get("groups", []):
                if (not isinstance(record, dict) or record.keys() != {"id", "title", "member_ids"}
                        or not isinstance(record["member_ids"], list)):
                    raise MindMapError("旧图分组必须包含标识、标题及成员标识列表")
                key, title, members = record["id"], record["title"], tuple(record["member_ids"])
                validate_legacy_graph_group(key, title, members, ids, group_ids, grouped_ids)
                legacy.append((key, title, members))
                group_ids.add(key)
                grouped_ids.update(members)
            return upgrade_legacy_graph_groups(graph, legacy)
        groups = {}
        grouped_ids = set()
        image_ids = {node.id for node in nodes if node.image_id is not None}
        required_group = {"id", "member_ids"}
        for record in data.get("groups", []):
            if (not isinstance(record, dict) or not required_group <= record.keys() <= required_group | {"min_size"}
                    or not isinstance(record["member_ids"], list)
                    or ("min_size" in record and not isinstance(record["min_size"], list))):
                raise MindMapError("图分组必须包含节点标识及成员标识列表")
            size = tuple(record["min_size"]) if "min_size" in record else None
            group = GraphGroup(record["id"], tuple(record["member_ids"]), size)
            validate_graph_group(group, node_ids=ids, group_ids=groups, grouped_ids=grouped_ids, image_ids=image_ids)
            if size is not None:
                group = replace(group, min_size=tuple(map(float, size)))
            groups[group.id] = group
            grouped_ids.update(group.member_ids)
        graph = replace(graph, groups=tuple(groups.values()))
        parents = graph.parent_groups()
        if (cycle := graph_membership_cycle(parents)) is not None:
            raise MindMapError(f"分组不能包含自身或形成循环：{cycle}")
        for edge in graph.edges:
            if not graph_edge_in_scope(edge, parents):
                raise MindMapError(f"连线的两端必须处于同一层：{edge.key}")
        return graph


def validate_graph_group_title(title: object) -> None:
    """校验组名为非空的单行 UTF-8 文本，失败时抛出 MindMapError。"""
    if not isinstance(title, str) or not title.strip() or title.splitlines() != [title]:
        raise MindMapError("分组名称必须是非空的单行文本")
    try:
        title.encode("utf-8")
    except UnicodeError as error:
        raise MindMapError("分组名称包含无效的 Unicode 字符") from error


def _validate_group_members(member_ids, node_ids, grouped_ids):
    members = set()
    for key in member_ids:
        if not isinstance(key, str) or key not in node_ids:
            raise MindMapError(f"分组引用了未声明的节点：{key!r}")
        if key in members or key in grouped_ids:
            raise MindMapError(f"分组成员重复：{key}")
        members.add(key)


def validate_graph_group(
    group: GraphGroup, *, node_ids: Collection[str], group_ids: Collection[str],
    grouped_ids: Collection[str], image_ids: Collection[str] = (),
) -> None:
    """核对组节点、成员和可选尺寸。各集合只读，失败抛出 MindMapError。

    group_ids 和 grouped_ids 记录已核对的分组及成员，image_ids 标识不能承载分组的
    纯图片节点。完整父链的成环检查在收集全部分组后执行。
    """
    from .validation import finite

    if not isinstance(group.id, str) or group.id not in node_ids or group.id in group_ids:
        raise MindMapError(f"分组节点标识无效或重复：{group.id!r}")
    if group.id in image_ids:
        raise MindMapError(f"纯图片节点不能承载分组：{group.id}")
    _validate_group_members(group.member_ids, node_ids, grouped_ids)
    size = group.min_size
    if size is not None and (len(size) != 2 or not all(
            finite(value) and 0 < value <= _MAX_GRAPH_COORDINATE for value in size)):
        raise MindMapError(f"分组最小宽高无效：{group.id}")


def validate_legacy_graph_group(key, title, members, node_ids, group_ids, grouped_ids) -> None:
    """按旧图的独立组命名空间校验单条分组记录，供旧原生记录和旧图文本共用。"""
    if not isinstance(key, str) or not re.fullmatch(GRAPH_NODE_ID, key) or key in group_ids:
        raise MindMapError(f"分组标识无效或重复：{key!r}")
    validate_graph_group_title(title)
    if not members:
        raise MindMapError(f"分组至少需要一个成员：{key}")
    _validate_group_members(members, node_ids, grouped_ids)


def graph_membership_cycle(parents: Mapping[str, str]) -> str | None:
    """迭代检查直接父组映射，返回环中一个标识。无环时返回 None。"""
    done = set()
    for key in parents:
        path = set()
        current = key
        while current in parents and current not in done:
            if current in path:
                return current
            path.add(current)
            current = parents[current]
        done.update(path)
    return None


def graph_edge_in_scope(edge: GraphEdge, parents: Mapping[str, str]) -> bool:
    """检查已存在的两个端点是否同层。顶层端点在 parents 中均无条目。"""
    return parents.get(edge.source) == parents.get(edge.target)


def promote_graph_edges(edges: Iterable[GraphEdge], parents: Mapping[str, str]) -> tuple[GraphEdge, ...]:
    """把越层线提升到最近的共同父层，保留方向及去重前首条边的顺序。

    parents 必须已校验为无环的直接父组映射。同层线直接保留。祖先与后代之间的线
    无法提升为两个同层端点，此时抛出 MindMapError，调用方应取消整批结构变更。
    """
    def lineage(key: str) -> list[str]:
        path = [key]
        while key in parents:
            key = parents[key]
            path.append(key)
        return path[::-1]

    result = {}
    for edge in edges:
        if graph_edge_in_scope(edge, parents):
            result[edge] = None
            continue
        for source, target in zip(lineage(edge.source), lineage(edge.target)):
            if source != target:
                result[GraphEdge(source, target)] = None
                break
        else:
            raise MindMapError(f"无法转接连线 {edge.key}，两端已成为祖先与后代")
    return tuple(result)


def available_graph_group_ids(node_ids: Iterable[str]) -> Iterator[str]:
    """按 g1、g2 等顺序生成当前节点命名空间可用的标识。"""
    used = set(node_ids)
    for index in count(1):
        key = f"g{index}"
        if key not in used:
            yield key


def upgrade_legacy_graph_groups(
    graph: NodeGraph, legacy: Collection[tuple[str, str, tuple[str, ...]]],
) -> NodeGraph:
    """把已校验的旧分组转换成有正文的图节点，保持普通节点、位置和附页标识。

    旧分组的独立标识若与节点冲突，按声明顺序使用空闲标识。现有越层连线提升到
    新的组节点，组内线保留实际端点。返回的新图可按当前版本写出。
    """
    occupied = {node.id for node in graph.nodes}
    ids = available_graph_group_ids(occupied | {key for key, _, _ in legacy})
    nodes, groups = [], []
    for key, title, members in legacy:
        group_id = next(ids) if key in occupied else key
        nodes.append(GraphNode(group_id, title))
        groups.append(GraphGroup(group_id, members))
    updated = replace(graph, nodes=(*graph.nodes, *nodes), groups=tuple(groups))
    return replace(updated, edges=promote_graph_edges(graph.edges, updated.parent_groups()))


def validate_graph_title(title: object) -> None:
    """校验标题可进入 UTF-8 原生文档；空正文与普通导图节点一样有效。"""
    if not isinstance(title, str):
        raise MindMapError("图节点标题必须是双引号字符串")
    try:
        title.encode("utf-8")
    except UnicodeError as error:
        raise MindMapError("图节点标题包含无效的 Unicode 字符") from error
