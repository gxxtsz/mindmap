"""节点专用详情页的通用数据边界；不导入 Qt、宿主或任何具体扩展。"""

import json
import re
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote, unquote

from .errors import MindMapError
from .graph import NodeGraph

if TYPE_CHECKING:
    from .models import MindMapDocument

GRAPH_PAGE_TYPE = "graph"


def graph_members(record: dict | None) -> dict[str, str]:
    """从已校验的图记录提取文档节点 ID 到局部 ID 的映射。

    缺少图或版本一返回空字典。调用方可以修改返回值，原生记录仍归文档所有。
    """
    if record is None or record["version"] == 1:
        return {}
    return {node["document_node_id"]: node["id"] for node in record["data"]["nodes"]
            if "document_node_id" in node}


def graph_page_id(node_id: str) -> str:
    """以所属文字节点的标识生成图页导航键；图内局部标识不进入页面路径。"""
    return f"@graph/{quote(node_id, safe='', errors='surrogatepass')}"


def parse_graph_page_id(page_id: object) -> str | None:
    """仅解析规范的图页键，返回所属节点标识；普通页或无效键返回 None。"""
    if not isinstance(page_id, str) or not page_id.isascii() or not page_id.startswith("@graph/"):
        return None
    try:
        owner = unquote(page_id.removeprefix("@graph/"), errors="surrogatepass")
    except UnicodeDecodeError:
        return None
    return owner if owner and graph_page_id(owner) == page_id else None


def extension_page_id(node_id: str, type_id: str) -> str:
    """构造只用于导航的稳定标识；编码节点 ID，避免路径分隔符歧义。"""
    return f"@extension/{quote(node_id, safe='')}/{type_id}"


def parse_extension_page_id(page_id: object) -> tuple[str, str] | None:
    """解析扩展导航标识；普通页面或无效输入返回 None，不读取文档。"""
    if not isinstance(page_id, str) or not page_id.startswith("@extension/"):
        return None
    parts = page_id.split("/")
    if (len(parts) != 3 or not parts[1] or parts[2] == GRAPH_PAGE_TYPE
            or not re.fullmatch(r"[a-z][a-z0-9._-]{0,99}", parts[2])):
        return None
    return unquote(parts[1]), parts[2]


def validate_extension_pages(pages: object, *, json_data: bool = True) -> None:
    """检查页面封装，不解释业务字段，也不要求对应的扩展已经安装。

    类型标识使用稳定的小写名称（可包含点、下划线和连字符）。每个节点的每种
    类型最多保存一页；版本必须为正整数，业务数据必须是 JSON 对象。拒绝循环、
    非有限数字和非字符串键，避免一次保存后数据含义改变。
    """
    if not isinstance(pages, dict):
        raise MindMapError("节点扩展页面必须为对象")
    for type_id, record in pages.items():
        if not isinstance(type_id, str) or not re.fullmatch(r"[a-z][a-z0-9._-]{0,99}", type_id):
            raise MindMapError("扩展页面类型标识无效")
        if not isinstance(record, dict) or set(record) != {"version", "data"}:
            raise MindMapError("扩展页面必须包含 version 和 data")
        if type(record["version"]) is not int or record["version"] < 1:
            raise MindMapError("扩展页面版本必须为正整数")
        if not isinstance(record["data"], dict):
            raise MindMapError("扩展页面数据必须为对象")
        if type_id == GRAPH_PAGE_TYPE:
            NodeGraph.from_data(record["data"], version=record["version"])
    if json_data and pages:
        try:
            # 编码先检测循环和不支持的值，再迭代检查键，避免递归循环或隐式键转换。
            json.dumps(pages, allow_nan=False)
            pending = [pages]
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    if any(not isinstance(key, str) for key in value):
                        raise ValueError("对象的键必须为字符串")
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
                elif type(value) not in (str, int, float, bool, type(None)):
                    raise ValueError("页面只接受 JSON 值")
        except (TypeError, ValueError, RecursionError) as error:
            raise MindMapError(f"扩展页面必须是有效的 JSON 数据：{error}") from error


class NodePagesMixin:
    """引擎的节点页面接口，全部写入经过现有事务、撤销历史与变更通知。"""

    def extension_page(self, node_id: str, type_id: str) -> dict | None:
        """返回独立页面快照；节点不存在时报错，尚未创建页面时返回 None。"""
        return deepcopy(self._editor.node(node_id).extension_pages.get(type_id))

    def node_graph(self, node_id: str) -> NodeGraph | None:
        """读取所属节点的一张图，返回独立快照；未挂载时返回 None，节点无效时报错。"""
        record = self._editor.node(node_id).extension_pages.get(GRAPH_PAGE_TYPE)
        return NodeGraph.from_data(record["data"], version=record["version"]) if record is not None else None

    def graph_node_location(self, document_node_id: str | None) -> tuple[str, str] | None:
        """按文档节点 ID 查询图内位置，返回（图所属节点 ID，局部 ID）。

        普通树节点、已删除节点和未绑定的图节点没有索引项，返回 None。查询只读
        运行时索引，不遍历图或改变历史。
        """
        return self._state.graph_nodes.get(document_node_id)

    def can_create_node_pages(self, page_id: str | None) -> bool:
        """查询页面类型是否允许创建详情、子导图和图。

        沿图的所属节点逐层查到树页，详情及其旧图返回 False。无效页面也返回
        False；只读、节点选择等界面状态由调用方校验。
        """
        return self._state.can_create_node_pages(page_id)

    def graph_node_document_id(self, owner_id: str, local_id: str) -> str | None:
        """按图所属节点和局部 ID 查询附页实体；存在但未绑定时返回 None。

        图或节点不存在时抛出 MindMapError。不同图可使用相同局部 ID，调用者必须
        同时提供所属节点；查询不创建实体，也不读取其他图。
        """
        graph = self.node_graph(owner_id)
        node = next((entry for entry in graph.nodes if entry.id == local_id), None) if graph else None
        if node is None:
            raise MindMapError("图节点不存在")
        return node.document_node_id

    def create_graph_node_page(
        self, owner_id: str, local_id: str, *, kind: Literal["detail", "submap", "graph"],
    ) -> str:
        """为指定图节点创建详情页、子导图或下级图，返回可跨视图使用的页面 ID。

        首次创建时在同一事务内分配文档节点 ID；后续创建复用该实体。目标须存在，
        同类页面只能创建一次，详情页中的旧图禁止创建。失败回滚节点、页面及索引，
        成功只记录一次历史。由宿主决定是否切页。
        """
        def action(owner_id, local_id, kind, page_id):
            if page_id is None:
                raise MindMapError("图的所属节点标识无效")
            if kind not in ("detail", "submap", "graph"):
                raise MindMapError("图节点的附页类型无效")
            self._editor.require_page_owner(owner_id)
            graph = self.node_graph(owner_id)
            entry = next((entry for entry in graph.nodes if entry.id == local_id), None) if graph else None
            if entry is None:
                raise MindMapError("图节点不存在")
            document_id = entry.document_node_id
            if document_id is None:
                from .models import Node

                node = Node(entry.title)
                self._state.put_node(node)
                document_id = node.id
                graph = replace(graph, nodes=tuple(
                    replace(entry, document_node_id=document_id) if entry.id == local_id else entry
                    for entry in graph.nodes))
                self._state.set_graph(owner_id, graph.to_record())
            if kind == "detail":
                return self._editor.create_detail_page(document_id)
            if kind == "submap":
                return self._editor.create_submap(document_id)
            if self.node_graph(document_id) is not None:
                raise MindMapError("此节点已有图")
            self._state.set_graph(document_id, NodeGraph().to_record())
            return graph_page_id(document_id)

        operation = ("create_detail_page" if kind == "detail" else
                     "create_submap" if kind == "submap" else "set_node_graph")
        return self._execute(operation, action, owner_id=owner_id, local_id=local_id, kind=kind,
                             page_id=graph_page_id(owner_id) if isinstance(owner_id, str) and owner_id else None)

    def set_node_graph(self, node_id: str, graph: NodeGraph) -> None:
        """创建或完整替换节点图；校验失败回滚事务，成功一次进入导图撤销历史。

        同一局部 ID 保留既有附页；标题变化同步到关联的树页根。删掉图节点会同时
        删除其附页。新绑定由创建附页或粘贴操作建立，调用方不能直接指定归属。
        详情页内只允许更新已存在的旧图。成功后历史包含图及全部受影响的文档实体。
        """
        def action(node_id, graph, page_id):
            if page_id is None:
                raise MindMapError("图的所属节点标识无效")
            if not isinstance(graph, NodeGraph):
                raise MindMapError("节点图必须是 NodeGraph")
            owner = self._editor.node(node_id)
            if owner.is_image_node:
                raise MindMapError("纯图片节点不能拥有图")
            previous = self.node_graph(node_id)
            if previous is None:
                self._editor.require_page_owner(node_id)
            self._editor.replace_node_graph(node_id, previous or NodeGraph(), graph)

        page_id = graph_page_id(node_id) if isinstance(node_id, str) and node_id else None
        self._execute("set_node_graph", action, node_id=node_id, graph=graph, page_id=page_id)

    def remove_node_graph(self, node_id: str) -> None:
        """删除节点拥有的图及图内附页，入口节点的其他页面保留。

        整组实体和图片资源在同一事务删除；一次撤销恢复原来的页面标识与内容。
        """
        def action(node_id, page_id):
            if page_id is None:
                raise MindMapError("图的所属节点标识无效")
            graph = self.node_graph(node_id)
            if graph is None:
                raise MindMapError("该节点没有图")
            self._editor.replace_node_graph(node_id, graph, None)

        page_id = graph_page_id(node_id) if isinstance(node_id, str) and node_id else None
        self._execute("remove_node_graph", action, node_id=node_id, page_id=page_id)

    def copy_graph_fragment(self, owner_id: str, node_ids: Iterable[str]) -> "MindMapDocument":
        """复制指定图的局部节点、内部连线及附属内容，返回独立的剪贴板文档。

        文档的合成根承载复制的图，图片资源随附页复制。调用不会建立新绑定、
        改变选择或写入历史；失效图及局部 ID 抛出 MindMapError。
        """
        graph = self.node_graph(owner_id)
        if graph is None:
            raise MindMapError("该节点没有图")
        return self._editor.copy_graph_fragment(graph, node_ids)

    def paste_graph_fragment(self, owner_id: str, fragment: "MindMapDocument") -> list[str]:
        """向指定图粘贴独立片段，返回新分配的局部节点 ID，整个操作可一次撤销。

        片段必须由单个合成根承载图；校验通过后在目标中复制页面、图节点绑定
        和内部引用。图片按内容哈希复用。失败回滚所有修改，片段保持原样。
        """
        def action(node_id, fragment, page_id):
            from .models import MindMapDocument

            if page_id is None:
                raise MindMapError("图的所属节点标识无效")
            if type(fragment) is not MindMapDocument:
                raise MindMapError("图剪贴板片段无效")
            fragment.validate()
            root = fragment.root
            record = root.extension_pages.get(GRAPH_PAGE_TYPE)
            if (record is None or root.children or root.submap_id or root.detail_page_id
                    or fragment.inspiration.root_ids):
                raise MindMapError("图剪贴板必须由一个节点图组成")
            current = self.node_graph(node_id)
            if current is None:
                raise MindMapError("该节点没有图")
            source = NodeGraph.from_data(record["data"], version=record["version"])
            if not source.nodes:
                return []
            return self._editor.paste_graph_fragment(node_id, current, fragment, source)

        return self._execute("paste_graph_fragment", action, node_id=owner_id, fragment=fragment,
                             page_id=graph_page_id(owner_id) if isinstance(owner_id, str) and owner_id else None)

    def set_extension_page(self, node_id: str, type_id: str, data: dict, *, version: int = 1) -> None:
        """创建或替换节点上的一页；成功产生一条历史，失败完全回滚。

        普通节点、子导图节点和普通详情页节点均可拥有专用详情页。纯图片节点
        沿用不允许附页的限制。输入在事务入口复制，不持有调用方的可写容器。
        """
        record = {"version": version, "data": data}

        def action(node_id, type_id, record):
            if type_id == GRAPH_PAGE_TYPE:
                raise MindMapError("请使用节点图接口修改图")
            self._write_node_page(node_id, type_id, record)

        self._execute("set_extension_page", action, node_id=node_id, type_id=type_id, record=record)

    def remove_extension_page(self, node_id: str, type_id: str) -> None:
        """删除指定类型的专用详情页；删除可撤销，其他类型的页面不受影响。"""
        def action(node_id, type_id):
            if type_id == GRAPH_PAGE_TYPE:
                raise MindMapError("请使用节点图接口删除图")
            self._delete_node_page(node_id, type_id)

        self._execute("remove_extension_page", action, node_id=node_id, type_id=type_id)

    def _write_node_page(self, node_id, type_id, record):
        # 校验置于事务内，外层批量操作即使捕获校验异常，先前的修改也会回滚。
        validate_extension_pages({type_id: record})
        node = self._editor.node(node_id)
        if node.is_image_node:
            raise MindMapError("纯图片节点不能拥有节点页")
        if node.extension_pages.get(type_id) != record:
            self._editor.edit(node_id).extension_pages[type_id] = deepcopy(record)

    def _delete_node_page(self, node_id, type_id):
        if type_id not in self._editor.node(node_id).extension_pages:
            raise MindMapError("该节点没有图" if type_id == GRAPH_PAGE_TYPE else "该节点没有此扩展详情页")
        self._editor.edit(node_id).extension_pages.pop(type_id)
