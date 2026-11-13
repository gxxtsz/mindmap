"""节点专用详情页的通用数据边界；不导入 Qt、宿主或任何具体扩展。"""

import json
import re
from copy import deepcopy
from urllib.parse import quote, unquote

from .errors import MindMapError


def extension_page_id(node_id: str, type_id: str) -> str:
    """构造只用于导航的稳定标识；编码节点 ID，避免路径分隔符歧义。"""
    return f"@extension/{quote(node_id, safe='')}/{type_id}"


def parse_extension_page_id(page_id: object) -> tuple[str, str] | None:
    """解析扩展导航标识；普通页面或无效输入返回 None，不读取文档。"""
    if not isinstance(page_id, str) or not page_id.startswith("@extension/"):
        return None
    parts = page_id.split("/")
    if len(parts) != 3 or not parts[1] or not re.fullmatch(r"[a-z][a-z0-9._-]{0,99}", parts[2]):
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

    def set_extension_page(self, node_id: str, type_id: str, data: dict, *, version: int = 1) -> None:
        """创建或替换节点上的一页；成功产生一条历史，失败完全回滚。

        普通节点、子导图节点和普通详情页节点均可拥有专用详情页。纯图片节点
        沿用不允许附页的限制。输入在事务入口复制，不持有调用方的可写容器。
        """
        record = {"version": version, "data": data}

        def action(node_id, type_id, record):
            # 校验也属于事务：外层批量操作即使捕获了异常，仍须整体回滚，不能
            # 留下“前半段成功、扩展页失败”的部分提交。
            validate_extension_pages({type_id: record})
            node = self._editor.node(node_id)
            if node.is_image_node:
                raise MindMapError("纯图片节点不能拥有扩展详情页")
            if node.extension_pages.get(type_id) != record:
                self._editor.edit(node_id).extension_pages[type_id] = deepcopy(record)

        self._execute("set_extension_page", action, node_id=node_id, type_id=type_id, record=record)

    def remove_extension_page(self, node_id: str, type_id: str) -> None:
        """删除指定类型的专用详情页；删除可撤销，其他类型的页面不受影响。"""
        def action(node_id, type_id):
            if type_id not in self._editor.node(node_id).extension_pages:
                raise MindMapError("该节点没有此扩展详情页")
            self._editor.edit(node_id).extension_pages.pop(type_id)

        self._execute("remove_extension_page", action, node_id=node_id, type_id=type_id)
