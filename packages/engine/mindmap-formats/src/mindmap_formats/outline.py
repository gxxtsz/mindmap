"""Markdown、单根文本大纲及多根缩进片段；只交换层级和文字，属于有损格式。"""

import re
from copy import deepcopy

from mindmap_core import MindMapDocument, MindMapError, Node

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(?:[-*+] |\d+[.)] )(.*)$")


def detect_outline_format(text: str) -> str:
    """只读识别剪贴板大纲，返回注册表中的 ``markdown`` 或 ``text`` 格式标识。

    text 为未经去缩进的完整正文；忽略开头 BOM 和空行，以首个非空行是否符合本包
    支持的 Markdown 标题、无序列表或有序列表语法决定格式。普通的 #、- 等正文
    前缀不会触发 Markdown；空白内容返回 text，由对应解析器给出空内容错误。
    本函数只判别格式，不校验后续行；调用方不得在所选格式解析失败后回退到另一格式。
    """
    first = next((line.strip() for line in text.removeprefix("\ufeff").splitlines() if line.strip()), "")
    return "markdown" if _HEADING.match(first) or _BULLET.match(first) else "text"


def _indentation_depths(
    records: list[tuple[int, str, str]], spaces_per_level: int,
) -> tuple[list[int], str | None]:
    """尝试一种空格单位，返回合法前缀的深度及首个错误；不创建任何文档实体。"""
    # Tab 固定贡献一级，而不是扩展到下一个制表位；混用时字符顺序不改变层级。
    widths = [prefix.count("\t") * spaces_per_level + prefix.count(" ") for _, prefix, _ in records]
    baseline = min(widths)
    depths = []
    for (line_number, _, _), width in zip(records, widths):
        relative = width - baseline
        if relative % spaces_per_level:
            return depths, f"第 {line_number} 行：缩进必须为每级 {spaces_per_level} 个空格，或使用 Tab"
        depth = relative // spaces_per_level
        if not depths and depth:
            return depths, f"第 {line_number} 行：首个节点缺少父节点，请包含父节点或统一整体缩进"
        if depths and depth > depths[-1] + 1:
            return depths, f"第 {line_number} 行：层级跳跃，缺少父节点"
        depths.append(depth)
    return depths, None


def import_indented_fragment(text: str) -> MindMapDocument:
    """将一行一个节点的纯文本解析为独立的多根剪贴板片段，不修改任何宿主状态。

    text 可包含 BOM、常见换行符、空行及整段共同缩进；只把行首空格和 Tab 当作缩进，
    行尾空白会去除，正文内部的空格、Tab 及符号均保留，不解释 Markdown 或树状线条。
    一个 Tab 始终表示一级；分别按每级 2／4 空格换算，减去各解释下所有非空行的最小
    缩进后校验父子关系。混用只在树形能够唯一确定时接受，两种解释树形相同也可接受。
    返回值使用不可见的「剪贴板」合成根，root.children 按原文顺序包含所有顶层节点，
    可直接交给 paste_fragment；每个正文节点使用独立的新标识与默认样式副本。
    空内容、缩进不整齐、缺失父节点、跳级或歧义抛出中文 MindMapError；错误行号从原文
    第 1 行计数并包含空行。两种解释均失败时报告合法前缀最长者的错误，并列采用 2 空格。
    完整校验通过后才建立片段；本函数不读取剪贴板、不修改文件、文档或撤销历史。
    """
    records = []
    for line_number, line in enumerate(text.removeprefix("\ufeff").splitlines(), 1):
        if not line.strip():
            continue
        prefix_length = len(line) - len(line.lstrip(" \t"))
        records.append((line_number, line[:prefix_length], line[prefix_length:].rstrip()))
    if not records:
        raise MindMapError("大纲内容不能为空")

    candidates = [_indentation_depths(records, unit) for unit in (2, 4)]
    valid = [depths for depths, error in candidates if error is None]
    if not valid:
        # max 在同分时保留首项，落实并列采用 2 空格解释的稳定报错约定。
        _, error = max(candidates, key=lambda candidate: len(candidate[0]))
        raise MindMapError(error)
    if len(valid) == 2 and valid[0] != valid[1]:
        line_number = next(
            record[0] for record, first, second in zip(records, *valid) if first != second
        )
        raise MindMapError(f"第 {line_number} 行：Tab 与空格混用导致层级存在歧义，请统一缩进方式")

    fragment = MindMapDocument.new("剪贴板")
    parents = [fragment.root_id]
    for (_, _, value), depth in zip(records, valid[0]):
        node = Node(value, style=deepcopy(fragment.root.style))
        fragment.nodes[node.id] = node
        fragment.nodes[parents[depth]].children.append(node.id)
        parents = parents[:depth + 1] + [node.id]
    fragment.validate()
    return fragment


def export_outline(document: MindMapDocument, *, markdown: bool = True) -> str:
    """导出层级与文本；节点样式、标签、备注和图片附件不属于大纲交换格式。"""
    document.validate()
    lines, stack = [], [(document.root_id, 0)]
    while stack:
        node_id, depth = stack.pop()
        node = document.nodes[node_id]
        text = node.text.replace("\r", "").replace("\n", " ")
        prefix = ("# " if depth == 0 else "  " * (depth - 1) + "- ") if markdown else "  " * depth
        lines.append(prefix + text)
        stack.extend((child, depth + 1) for child in reversed(node.children))
    return "\n".join(lines) + "\n"


def import_outline(text: str, *, markdown: bool = True) -> MindMapDocument:
    """解析标题/列表 Markdown 或每两空格一级的缩进文本。

    首个条目作为根节点；列表可接在标题后。空行忽略，缩进或层级错误以中文
    MindMapError 报告原文行号，避免凭空补出无内容的节点。保持单根文档的原有
    返回约定；多根纯文本粘贴请使用 import_indented_fragment。本函数只返回新文档，
    不修改宿主或历史，也不尝试解析完整 Markdown 文档语义。
    """
    records = []
    heading_base = None
    for line_number, line in enumerate(text.removeprefix("\ufeff").expandtabs(2).splitlines(), 1):
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        value = line.strip()
        depth = indent // 2
        if markdown:
            heading = _HEADING.match(value)
            bullet = _BULLET.match(value)
            if heading:
                level = len(heading[1])
                heading_base = level if heading_base is None else heading_base
                depth, value = level - heading_base, heading[2]
            elif bullet:
                depth, value = indent // 2 + (1 if heading_base is not None else 0), bullet[1]
            elif records:
                raise MindMapError(f"第 {line_number} 行：Markdown 大纲只支持标题或列表条目")
        if indent % 2:
            raise MindMapError(f"第 {line_number} 行：大纲缩进必须为两个空格的整数倍")
        records.append((line_number, depth, value))
    if not records:
        raise MindMapError("大纲内容不能为空")
    initial_depth = records[0][1]
    document = MindMapDocument.new(records[0][2])
    parents = [document.root_id]
    for line_number, raw_depth, value in records[1:]:
        depth = raw_depth - initial_depth
        if depth == 0:
            # 没有根标题的列表以首项为根，其余顶层条目作为根的子节点。
            depth = 1
        if depth < 1 or depth > len(parents):
            raise MindMapError(f"第 {line_number} 行：大纲层级跳跃或早于根标题")
        # 大纲只携带结构和正文，导入时为每一层独立复制新导图的默认外观。
        # 不能只给根主题设置圆角正交，否则更深层级仍会落回原始曲线。
        node = Node(value, style=deepcopy(document.root.style))
        document.nodes[node.id] = node
        document.nodes[parents[depth - 1]].children.append(node.id)
        parents = parents[:depth] + [node.id]
    document.validate()
    return document
