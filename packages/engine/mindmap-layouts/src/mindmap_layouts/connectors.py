"""与 Qt 无关的父子连接线路由；节点排布、路径生成和描画属性各自独立。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
from math import copysign, hypot, isfinite, sin

from mindmap_core import (
    BranchPath,
    BranchStyle,
    Curve,
    MindMapDocument,
    MindMapError,
    PathCommand,
    Point,
    Rect,
    get_branch_style,
)

_EPSILON = 1e-8


def recovered_curve_weights(advance: float, offset: float) -> tuple[float, float]:
    """普通分支的坡度自适应系数，来自 calcLinePath 的 case2/case10。

    证据 ev_154960d96c32553331bfb19d6c801745e2fb011bb1b9d9db4e1bc138d16fb966，
    地址 0x180218b50。这里的起终点由本项目矩形确定，未移植原生特殊节点补偿。
    """
    if abs(advance) <= 1 or abs(offset / advance) < 0.1:
        return 0.3, 0.4
    slope = abs(offset / advance)
    if slope >= 4:
        return 1.0, 0.7
    weight = sin(slope * 0.39269875)
    return 0.3 + 0.7 * weight, 0.4 + 0.3 * weight


def _local_route(kind: BranchStyle, advance: float, offset: float) -> tuple[list[PathCommand], Point]:
    """先生成向右局部路径和终点切向，后续一次性变换全部控制点与箭头。"""
    end = (advance, offset)
    commands = [PathCommand("M", ((0.0, 0.0),))]

    def line(point):
        commands.append(PathCommand("L", (point,)))

    def cubic(first, second, last=end):
        commands.append(PathCommand("C", (first, second, last)))

    if abs(offset) <= _EPSILON or advance <= _EPSILON:
        line(end)
        return commands, end if hypot(*end) > _EPSILON else (1.0, 0.0)
    if kind == BranchStyle.LINE:
        line(end)
        return commands, end
    # 主干位置 0.35、圆滑分叉主干 0.18 为本项目的独立选择，区别于恢复出的曲线常量。
    trunk = advance * 0.35
    if kind in (BranchStyle.ELBOW, BranchStyle.ARROW):
        line((trunk, 0.0))
        line((trunk, offset))
        line(end)
    elif kind == BranchStyle.ROUND:
        # 原生 case4 的半径上限 6 和控制点距角点 0.448r；只圆化子线出口，
        # 中央 T 形交汇保持直主干。半线段钳制为短边保护，防止圆角互相越过。
        radius = min(6.0, abs(offset) / 2, (advance - trunk) / 2)
        control = 0.448 * radius
        line((trunk, 0.0))
        line((trunk, offset - copysign(radius, offset)))
        cubic(
            (trunk, offset - copysign(control, offset)),
            (trunk + control, offset),
            (trunk + radius, offset),
        )
        line(end)
    elif kind == BranchStyle.ROUND2:
        # 原生 case6 的普通横向尾段为 11；间距不足时钳制到半间距。
        tail = min(11.0, advance / 2)
        line((advance - tail, offset))
        line(end)
    elif kind in (BranchStyle.ARC, BranchStyle.ARROW2):
        # 原生 case1/case9 共用中心线，箭头在路由完成后独立构造。
        cubic((0.0, offset * 0.4), (advance * 0.4, offset))
    elif kind == BranchStyle.ARC2:
        # 原生 case5 与上一组的控制比例不同，不能只改变起始主干来代替。
        cubic((0.0, offset * 0.2), (advance * 0.75, offset))
    elif kind == BranchStyle.CURVE:
        a, b = recovered_curve_weights(advance, offset)
        cubic((b * advance, 0.0), (advance - a * advance, offset))
    elif kind == BranchStyle.CENTRE:
        short_trunk = advance * 0.18
        remaining = advance - short_trunk
        a, b = recovered_curve_weights(remaining, offset)
        line((short_trunk, 0.0))
        cubic((short_trunk + b * remaining, 0.0), (advance - a * remaining, offset))
    else:
        # 第 11 类仍使用已验证的独立重建公式，不声称完整恢复了原生 case11。
        cubic((advance * 0.60, 0.0), (advance * 0.70, offset * 0.72))
        return commands, (advance * 0.30, offset * 0.28)
    return commands, (1.0, 0.0)


def _anchors(parent: Rect, child: Rect, direction: str) -> tuple[Point, Point, float, float]:
    if direction == "right":
        start, end = (parent.right, parent.center[1]), (child.x, child.center[1])
        return start, end, end[0] - start[0], end[1] - start[1]
    if direction == "left":
        start, end = (parent.x, parent.center[1]), (child.right, child.center[1])
        return start, end, start[0] - end[0], end[1] - start[1]
    if direction == "down":
        start, end = (parent.center[0], parent.bottom), (child.center[0], child.y)
        return start, end, end[1] - start[1], end[0] - start[0]
    raise MindMapError("分支路由方向必须为 right、left 或 down")


def route_branch(
    parent: Rect,
    child: Rect,
    kind: BranchStyle | str,
    *,
    direction: str = "right",
    color: str = "#3478d5",
    width: float = 1.8,
) -> BranchPath:
    """从父子矩形边缘路由一条显式样式分支，返回无界面依赖的不可变几何。

    左向镜像 X，向下交换 X/Y；端点、控制点和切向共用变换。独立调用保留完整主干，
    一次布局应调用 route_branches，以合并相同属性的重叠主干。
    """
    try:
        kind = BranchStyle(kind)
    except (TypeError, ValueError) as exc:
        raise MindMapError(f"不支持的分支连接线样式：{kind}") from exc
    if not isinstance(color, str) or not color:
        raise MindMapError("分支颜色不能为空")
    if isinstance(width, bool) or not isinstance(width, (int, float)) or not isfinite(width) or width <= 0:
        raise MindMapError("分支线宽必须为正有限数值")
    for rect in (parent, child):
        if (
            not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v)
                for v in (rect.x, rect.y, rect.width, rect.height)
            )
            or min(rect.width, rect.height) <= 0
        ):
            raise MindMapError("分支节点矩形无效")
    start, end, advance, offset = _anchors(parent, child, direction)
    if advance < -_EPSILON:
        raise MindMapError("子节点必须位于父节点的分支方向外侧")
    advance = max(0.0, advance)

    def transform(point):
        x, y = point
        dx, dy = (y, x) if direction == "down" else (-x, y) if direction == "left" else (x, y)
        return start[0] + dx, start[1] + dy

    local, tangent = _local_route(kind, advance, offset)
    commands = tuple(PathCommand(command.op, tuple(map(transform, command.points))) for command in local)
    arrow = None
    if kind in (BranchStyle.ARROW, BranchStyle.ARROW2):
        # 先变换切向再归一化，保证所有方向下箭头都沿最后一段，而不是沿父子连心线。
        tx, ty = transform(tangent)
        tx, ty = tx - start[0], ty - start[1]
        length = hypot(tx, ty)
        tx, ty = tx / length, ty / length
        arrow_length = min(10.0, hypot(advance, offset) * 0.28)
        half_width = arrow_length * 0.43
        base = end[0] - tx * arrow_length, end[1] - ty * arrow_length
        arrow = (
            end,
            (base[0] - ty * half_width, base[1] + tx * half_width),
            (base[0] + ty * half_width, base[1] - tx * half_width),
        )
    return BranchPath(start, end, commands, color, width, arrow)


def _deduplicate_straight_segments(branches: list[tuple[str, BranchPath]]) -> dict[str, BranchPath]:
    """相同父主题、方向、样式及画笔的直主干只描画一次，曲线和各自箭头保持独立。

    水平/竖直区间按首次出现的分支分配所有权；后续分支仅输出尚未覆盖的区间。
    因而子节点 ID 和逻辑锚点不变，也不会因为透明颜色重复叠加而加深主干。
    """
    seen = defaultdict(list)
    result = {}
    for key, branch in branches:
        output = []
        current = drawn = branch.start

        def move(point):
            nonlocal drawn
            if not output or drawn != point:
                output.append(PathCommand("M", (point,)))
            drawn = point

        for command in branch.commands:
            if command.op == "M":
                current = command.points[0]
                move(current)
            elif command.op == "L":
                end = command.points[0]
                if abs(end[1] - current[1]) < _EPSILON:
                    axis, constant, first, last = 0, current[1], current[0], end[0]
                elif abs(end[0] - current[0]) < _EPSILON:
                    axis, constant, first, last = 1, current[0], current[1], end[1]
                else:
                    move(current)
                    output.append(command)
                    current = drawn = end
                    continue
                low, high = sorted((first, last))
                intervals = seen[axis, constant]
                remaining = [(low, high)]
                for used_low, used_high in intervals:
                    remaining = [
                        piece
                        for a, b in remaining
                        for piece in ((a, min(b, used_low)), (max(a, used_high), b))
                        if piece[1] - piece[0] > _EPSILON
                    ]
                if last < first:
                    remaining = [(b, a) for a, b in reversed(remaining)]
                for a, b in remaining:
                    begin = (a, constant) if axis == 0 else (constant, a)
                    finish = (b, constant) if axis == 0 else (constant, b)
                    move(begin)
                    output.append(PathCommand("L", (finish,)))
                    drawn = finish
                merged = []
                for a, b in sorted([*intervals, (low, high)]):
                    if merged and a <= merged[-1][1] + _EPSILON:
                        merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
                    else:
                        merged.append((a, b))
                seen[axis, constant] = merged
                current = end
            else:
                move(current)
                output.append(command)
                current = drawn = command.points[-1]
        result[key] = replace(branch, commands=tuple(output))
    return result


def route_branches(document: MindMapDocument, nodes: Mapping[str, Rect]) -> dict[str, Curve | BranchPath]:
    """为本次可见矩形生成父子分支；样式只读取父节点，不隐式向后代继承。"""
    parents = document.parents()
    colors = {}
    for index, child in enumerate(document.root.children):
        for key in document.descendants(child):
            colors[key] = document.theme.palette[index % len(document.theme.palette)]
    result = {}
    groups = defaultdict(list)
    for key, child in nodes.items():
        parent_id = parents.get(key)
        if parent_id not in nodes:
            continue
        parent = nodes[parent_id]
        direction = (
            "down" if document.layout.direction == "down" else "left" if child.x < parent.x else "right"
        )
        color = colors.get(key, document.theme.accent)
        kind = get_branch_style(document.nodes[parent_id].style)
        if kind is None:
            # 完整保留原有默认四点曲线，包括向下转置后的控制点及旧画笔外观。
            start, end, _, _ = _anchors(parent, child, direction)
            if direction == "down":
                middle = (start[1] + end[1]) / 2
                points = start, (start[0], middle), (end[0], middle), end
            else:
                middle = (start[0] + end[0]) / 2
                points = start, (middle, start[1]), (middle, end[1]), end
            result[key] = Curve(points, color)
        else:
            branch = route_branch(parent, child, kind, direction=direction, color=color)
            result[key] = branch
            groups[parent_id, direction, kind, color, branch.width].append((key, branch))
    for group in groups.values():
        result.update(_deduplicate_straight_segments(group))
    return result
