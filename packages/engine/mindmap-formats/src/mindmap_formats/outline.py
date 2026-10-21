"""Markdown 与两空格缩进大纲；只交换层级和文字，属于有损格式。"""

import re
from copy import deepcopy

from mindmap_core import MindMapDocument, MindMapError, Node


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

    首个条目作为根节点；列表可接在标题后。空行忽略，层级跳跃时报错，
    避免凭空补出无内容的节点。此接口不尝试解析完整 Markdown 文档语义。
    """
    records = []
    heading_base = None
    for line in text.expandtabs(2).splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        value = line.strip()
        depth = indent // 2
        if markdown:
            heading = re.match(r"^(#{1,6})\s+(.*)$", value)
            bullet = re.match(r"^(?:[-*+] |\d+[.)] )(.*)$", value)
            if heading:
                level = len(heading[1])
                heading_base = level if heading_base is None else heading_base
                depth, value = level - heading_base, heading[2]
            elif bullet:
                depth, value = indent // 2 + (1 if heading_base is not None else 0), bullet[1]
            elif records:
                raise MindMapError("Markdown 大纲只支持标题或列表条目")
        if indent % 2:
            raise MindMapError("大纲缩进必须为两个空格的整数倍")
        records.append((depth, value))
    if not records:
        raise MindMapError("大纲内容不能为空")
    initial_depth = records[0][0]
    document = MindMapDocument.new(records[0][1])
    parents = [document.root_id]
    for raw_depth, value in records[1:]:
        depth = raw_depth - initial_depth
        if depth == 0:
            # 没有根标题的列表以首项为根，其余顶层条目作为根的子节点。
            depth = 1
        if depth < 1 or depth > len(parents):
            raise MindMapError("大纲层级跳跃或早于根标题")
        # 大纲只携带结构和正文，导入时为每一层独立复制新导图的默认外观。
        # 不能只给根主题设置圆角正交，否则更深层级仍会落回原始曲线。
        node = Node(value, style=deepcopy(document.root.style))
        document.nodes[node.id] = node
        document.nodes[parents[depth - 1]].children.append(node.id)
        parents = parents[:depth] + [node.id]
    document.validate()
    return document
