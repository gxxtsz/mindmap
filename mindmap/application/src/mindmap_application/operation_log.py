"""导图变更转为中文摘要，平台日志不解释导图事件。"""
from pathlib import PureWindowsPath

from mindmap_core import ChangeEvent, NodeMove


def describe_display(message):
    """导图业务决定日志标题和对象说明，完整摘要仍保留给复制及悬停详情。"""
    title, _, detail = message.partition("：")
    if title in {"撤销", "重做"}:
        action, _, detail = detail.partition("：")
        return f"{title} · {action}", detail
    if title in {"打开导图", "新建导图", "保存导图", "另存为"}:
        return title, ""
    if title == "已导出":
        return "导出图形", PureWindowsPath(detail).name
    return title, detail

_OPERATION_NAMES = {
    "add_inspiration": "新建想法", "set_inspiration_positions": "移动游离主题",
    "move_to_inspiration": "转为独立想法", "attach_inspirations": "加入导图",
    "group_inspirations": "创建共同父主题",
    "add_child": "新增子节点", "add_sibling": "新增同级节点", "insert_parent": "插入父节点",
    "update_node": "修改节点属性", "remove_nodes": "删除节点", "move_nodes": "移动节点",
    "move_up": "调整节点顺序", "set_expanded": "调整节点折叠状态",
    "set_layout": "修改导图布局", "set_theme": "修改导图主题", "set_branch_style": "修改分支样式",
    "add_image_child": "新增图片节点", "attach_image": "关联节点图片",
    "paste_fragment": "粘贴节点", "copy_nodes": "复制节点到导图",
    "create_submap": "创建子导图", "remove_submap": "删除子导图",
    "convert_descendants_to_submap": "后代转为子导图",
    "convert_submap_to_descendants": "子导图转为节点后代",
    "create_detail_page": "创建详情页", "remove_detail_page": "删除详情页",
    "set_submap_favorite": "修改子导图收藏",
    "set_page_reference": "设置页面引用",
    "set_extension_page": "更新扩展详情页", "remove_extension_page": "删除扩展详情页",
    "set_node_graph": "更新节点图", "remove_node_graph": "删除节点图",
}
_LEVEL_NAMES = {"info": "信息", "warning": "警告", "error": "错误"}


def _single_line(value: str, limit: int = 2048) -> str:
    """把用户文字压成单行并限制长度，保证一条操作始终只占一条日志记录。"""
    text = " ".join(str(value).splitlines())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _node_title(text: str, node_id: str, *, limit: int = 2048) -> str:
    """用提交时名称标识节点；无文字节点附带短标识，limit 为名称显示预算。"""
    name = _single_line(text, limit).strip() or f"无文字节点（{node_id[:8]}）"
    return f"「{name}」"


def _describe_move(move: NodeMove, event: ChangeEvent) -> str:
    """按本次事件的实际方向描述一个移动根，不借用当前文档或改变旧日志。

    跨父级明确列出来源与去向；同父级只说明发生变化的同级序号和有效方向。
    撤销交换固定快照的两端，因此“移回”及排序、换边方向与实际重放一致。
    """
    start, end = (move.before, move.after) if event.forward else (move.after, move.before)
    title = _node_title(end.node_text, move.node_id)
    if start.position != end.position and start.parent_id is None and end.parent_id is None:
        return f"更新独立想法{title}的兼容坐标至 {end.position}"
    source = _node_title(start.parent_text if start.parent_id is not None else "独立想法列表", start.parent_id)
    target = _node_title(end.parent_text if end.parent_id is not None else "独立想法列表", end.parent_id)
    if start.parent_id != end.parent_id:
        verb = "移回" if event.kind == "undo" else "移到"
        return f"将{title}从{source}{verb}{target}下"
    details = []
    if start.index != end.index:
        details.append(f"由第{start.index}项调整为第{end.index}项")
    if start.direction != end.direction:
        directions = {"left": "左侧", "right": "右侧", "down": "下方"}
        details.append(f"从{directions[start.direction]}移到{directions[end.direction]}")
    return f"{title}在{target}下" + "，".join(details) if details else ""


def _text_preview(value: str, limit: int, changed_at: int) -> str:
    """在字段预算内保留实际修改附近的内容，不让长公共前缀遮住文字差异。"""
    if not value:
        return "（空内容）"
    if len(value) <= limit:
        return value
    start = max(0, min(changed_at - 32, len(value) - limit + 1))
    prefix = "…" if start else ""
    available = limit - len(prefix)
    suffix = "…" if start + available < len(value) else ""
    return prefix + value[start:start + available - len(suffix)] + suffix


def _describe_text_changes(changes: list[tuple[str, str]], *, forward: bool, limit: int) -> str:
    """从事务两端的文字快照生成修改说明，返回受单条日志长度限制的纯文本。

    撤销交换旧值与新值；批量操作最多展示三组不同修改并说明剩余组数。别名链
    同步产生的相同修改由调用方合并，避免入口、子导图和详情页重复刷出同一句。
    新旧内容均分显示预算，空串显式标注；换行和制表符显示为 ↵、⇥，保持一条
    日志一行。长内容围绕首个差异取上下文，后续改名或删除不影响本次说明。
    """
    shown = changes[:3]
    suffix = f"；另有{len(changes) - 3}组文字修改" if len(changes) > 3 else ""
    field_limit = max(8, (limit - len(suffix) - len(shown) + 1
                          - len("从「」改为「」") * len(shown)) // (2 * len(shown)))
    descriptions = []
    for before, after in shown:
        before, after = (before, after) if forward else (after, before)
        # 不能直接压平换行，否则“甲\n乙”改为“甲 乙”会看起来没有变化。
        values = [text.replace("\r\n", "↵").replace("\r", "↵").replace("\n", "↵")
                  .replace("\u2028", "↵").replace("\u2029", "↵").replace("\t", "⇥")
                  for text in (before, after)]
        changed_at = 0
        if max(map(len, values)) > field_limit:
            changed_at = next((index for index, (old, new) in enumerate(zip(*values)) if old != new),
                              min(map(len, values)))
        old, new = [_text_preview(value, field_limit, changed_at) for value in values]
        descriptions.append(f"从「{old}」改为「{new}」")
    return "；".join(descriptions) + suffix


def _describe_node_structure(event: ChangeEvent) -> list[str]:
    """从实际增删的实体快照描述分支根及其直接父节点，不查询当前文档。

    起止端由 forward 决定；新增取结果端名称，删除取起始端名称，撤销删除写成
    恢复。只列出独立分支根，附属页根及随行后代不重复列为选中对象；总数按
    真正从无到有或从有到无的节点 ID 统计，包含连带页面节点，不包含父级的
    children 更新或图片资源。最多列出三处分支及其父名，始终保留完整总数。
    缺少父级快照的旧事件维持通用摘要，不根据现有选择或已删除文档猜测父级。
    """
    if event.changes is None:
        return []
    collections = event.changes.collections
    nodes = collections.get("nodes", {})
    start = 0 if event.forward else 1
    descriptions = []
    for added, side in ((True, 1 - start), (False, start)):
        changed = {key: pair[side] for key, pair in nodes.items()
                   if pair[side] is not None and pair[1 - side] is None}
        if not changed:
            continue
        snapshots = {key: pair[side] for key, pair in nodes.items() if pair[side] is not None}
        parents = {child: key for key, node in snapshots.items() for child in node.children}
        pages = {pair[side].root_id: (pair[side], "子导图" if name == "submaps" else "详情页")
                 for name in ("submaps", "detail_pages")
                 for pair in collections.get(name, {}).values() if pair[side] is not None}
        page_owners = {page.owner_id for key, (page, _) in pages.items() if key in changed}
        # 单独增删附属页时用“入口”说明归属；连带增删时仍归入所属分支，避免重复计根。
        references = {key: (page.owner_id, "入口", kind) for key, (page, kind) in pages.items()
                      if page.owner_id not in changed and page.owner_id in snapshots}
        roots = [key for key in changed if key in references or
                 key not in pages and key in parents and parents[key] not in changed]
        if not roots:
            continue
        verb = "恢复" if added and event.kind == "undo" else "新增" if added else "删除"
        branches = []
        for key in roots[:3]:
            node = changed[key]
            parent_id, owner_role, page_kind = references.get(key, (parents.get(key), "", ""))
            parent_title = _node_title(snapshots[parent_id].text, parent_id, limit=120)
            title = _node_title(node.text, key, limit=120)
            kind = page_kind or ("分支" if key in page_owners or any(child in changed for child in node.children) else "节点")
            branches.append(f"在{owner_role}{parent_title}下{verb}{kind}{title}" if added
                            else f"从{owner_role}{parent_title}下{verb}{kind}{title}")
        if len(roots) > 3:
            branches.append(f"另有{len(roots) - 3}处{verb}")
        summary = "；".join(branches)
        if not added or len(changed) > 1 or verb == "恢复":
            summary += f"，共{verb}{len(changed)}个节点"
        descriptions.append(summary)
    return descriptions


def describe_change(event: ChangeEvent) -> str:
    """从一次已提交事件生成中文摘要，不查询引擎或保留收到的变化集。

    仅供 change、undo、redo 事件使用；事务标签作为整体操作名，撤销及重做
    保留原操作的含义。增删使用该端的父子快照，仅列出独立根并统计实际节点数。
    移动信息使用事务记录的准确对象、前后父级和位置，
    不把被挤动的兄弟或随行后代误写成移动对象；缺少该信息时维持通用摘要。
    文字修改从实体差量读取旧值和新值，按重放方向描述，合并别名同步的相同修改。
    其他普通编辑的节点名称优先取新增或删除项，删除后仍可从差量读取旧名。
    所有遍历仅涉及本次变化实体，不为日志复制整张导图。
    """
    label = _OPERATION_NAMES.get(event.operation, event.operation)
    if label.isascii():
        label = "修改导图"
    prefix = {"undo": "撤销：", "redo": "重做："}.get(event.kind, "")
    descriptions = _describe_node_structure(event)
    if event.changes is not None and event.changes.moves:
        movements = [_describe_move(move, event) for move in event.changes.moves]
        descriptions.extend(text for text in movements if text)
    nodes = event.changes.collections.get("nodes", {}) if event.changes is not None else {}
    pairs = list(nodes.values())
    if event.operation == "set_page_reference" and pairs:
        old, new = pairs[0]
        if old is not None and new is not None:
            if new.page_reference_root_id is None:
                label = "解除页面引用"
            elif old.page_reference_root_id is not None:
                label = "更换引用页面"
    text_changes = list(dict.fromkeys((old.text, new.text) for old, new in pairs
                                      if old is not None and new is not None and old.text != new.text))
    if text_changes:
        if event.operation == "update_node":
            label = "修改节点文字"
        heading = prefix + label + "："
        available = 2048 - len(heading) - sum(len(text) + 1 for text in descriptions)
        descriptions.append(_describe_text_changes(text_changes, forward=event.forward, limit=available))
    if descriptions:
        return prefix + label + "：" + "；".join(descriptions)
    if event.operation == "update_node":
        if any(old and new and old.image_width != new.image_width for old, new in pairs):
            label = "调整图片大小"
    added = [new for old, new in pairs if old is None and new is not None]
    removed = [old for old, new in pairs if new is None and old is not None]
    targets = added or removed or [new or old for old, new in pairs]
    titles = ["「" + _single_line(node.text, 60) + "」" if node.text else "图片节点"
              for node in targets[:3]]
    if titles:
        label += "：" + "、".join(titles) + (f"等 {len(targets)} 个节点" if len(targets) > 3 else "")
    return prefix + label


