"""画布与灵感侧栏共享的内部拖放标识载荷，不包含节点副本或文件内容。"""

import json
from dataclasses import dataclass

from PySide6.QtCore import QMimeData

BRANCH_DRAG_MIME = "application/x-mindmap-branch-drag+json"


@dataclass(frozen=True)
class BranchDrag:
    """一次本机会话内的移动请求；接收方仍须校验权限、版本、归属和循环。"""

    token: str
    generation: int
    # 最近一次内容修订号；保存事件虽递增引擎修订号，但不改变此令牌。
    revision: int
    node_ids: tuple[str, ...]
    origin: str


def encode_branch_drag(payload: BranchDrag) -> QMimeData:
    """将不可变标识载荷编码为 Qt 拖放数据，返回由拖动源拥有的新 QMimeData。

    不复制节点、图片或附属页面；token 用于匹配同一画布会话，generation 和
    revision 使用最近一次内容修订，拒绝手势期间过期内容且不受保存事件影响。
    调用方仅在 GUI 线程交给 QDrag 使用。
    """
    mime = QMimeData()
    mime.setData(BRANCH_DRAG_MIME, json.dumps({
        "token": payload.token, "generation": payload.generation, "revision": payload.revision,
        "node_ids": list(payload.node_ids), "origin": payload.origin,
    }).encode("utf-8"))
    return mime


def decode_branch_drag(mime: QMimeData) -> BranchDrag | None:
    """只解析本组件的拖放格式；损坏、超大、重复或未知来源载荷返回 None。

    查询不修改 MIME 或文档，也不代表已授权移动；宿主必须再次验证对应会话、
    内容版本、节点归属和编辑状态。仅在 GUI 线程读取 Qt 对象。
    """
    raw = bytes(mime.data(BRANCH_DRAG_MIME))
    if not raw or len(raw) > 1_000_000:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    token, ids = data.get("token"), data.get("node_ids")
    generation, revision, origin = data.get("generation"), data.get("revision"), data.get("origin")
    if (not isinstance(token, str) or not token or type(generation) is not int or type(revision) is not int
            or origin not in ("sidebar", "canvas") or not isinstance(ids, list) or not ids
            or any(not isinstance(key, str) or not key for key in ids) or len(set(ids)) != len(ids)):
        return None
    if origin == "sidebar" and len(ids) != 1:
        return None
    return BranchDrag(token, generation, revision, tuple(ids), origin)
