"""无界面依赖的确定性四方向树布局；显式遍历避免深层树递归限制。"""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite

from mindmap_core import (
    BranchPath,
    LayoutConfig,
    LayoutInfo,
    LayoutResult,
    MindMapDocument,
    MindMapError,
    Point,
    Rect,
)

from .connectors import route_branches


class TreeLayout:
    """根据尺寸布局节点，measurements 以节点 ID 为键，值为包含图片的完整节点尺寸。

    实际画布应提供真实尺寸。省略测量时使用明确的近似值，供无 GUI 的结构分析使用。
    focus_id 只影响本次可见子树，expand_all 仅影响本次布局，不修改文档。
    """

    info = LayoutInfo(
        "tree",
        "树形布局",
        LayoutConfig(),
        directions=("both", "right", "left", "down"),
        supports_compact=True,
        supports_branch_styles=True,
    )

    def validate_config(self, config: LayoutConfig) -> None:
        """当前树布局没有专有选项，未知参数明确报错，避免静默忽略拼写错误。"""
        config.validate()
        if config.algorithm_id != self.info.id:
            raise MindMapError("树布局配置的算法标识无效")
        if config.options:
            raise MindMapError("树布局不支持额外布局选项")

    def layout(
        self,
        document: MindMapDocument,
        measurements: Mapping[str, Point] | None = None,
        *,
        expand_all: bool = False,
        focus_id: str | None = None,
    ) -> LayoutResult:
        document.validate()
        self.validate_config(document.layout)
        measurements = measurements or {}
        root = focus_id or document.root_id
        if root not in document.nodes:
            raise MindMapError("聚焦节点不存在")
        down = document.layout.direction == "down"
        gap_x, gap_y = (32.0, 10.0) if document.layout.compact else (64.0, 22.0)

        def size(key: str, text: str = "") -> Point:
            width, height = measurements.get(key, (max(64, min(280, 24 + len(text) * 8)), 36))
            if not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v) and v > 0
                for v in (width, height)
            ):
                raise MindMapError(f"节点测量尺寸无效：{key}")
            return (height, width) if down else (width, height)

        visible, children, stack = [], {}, [root]
        while stack:
            node_id = stack.pop()
            node = document.nodes[node_id]
            visible.append(node_id)
            children[node_id] = node.children if node.expanded or expand_all else []
            stack.extend(reversed(children[node_id]))
        sides = document.branch_sides()
        if focus_id:
            side = sides[root]
            sides = dict.fromkeys(visible, side)
        sizes = {key: size(key, document.nodes[key].text) for key in visible}
        heights = {}
        for node_id in reversed(visible):
            branch_height = sum(heights[child] for child in children[node_id])
            branch_height += max(0, len(children[node_id]) - 1) * gap_y
            heights[node_id] = max(sizes[node_id][1], branch_height)
        logical: dict[str, Rect] = {}
        root_w, root_h = sizes[root]
        logical[root] = Rect(-root_w / 2, -root_h / 2, root_w, root_h)
        # 根节点允许两侧同时排布，各侧独立垂直居中。其余节点沿所属分支方向布局。
        pending = [root]
        while pending:
            parent_id = pending.pop()
            parent = logical[parent_id]
            groups: dict[str, list[str]] = {}
            for child in children[parent_id]:
                side = sides[child]
                side = "right" if side == "down" else side
                groups.setdefault(side, []).append(child)
            for side, siblings in groups.items():
                total = sum(heights[child] for child in siblings) + (len(siblings) - 1) * gap_y
                top = parent.center[1] - total / 2
                for child in siblings:
                    width, height = sizes[child]
                    x = parent.x - gap_x - width if side == "left" else parent.right + gap_x
                    logical[child] = Rect(x, top + (heights[child] - height) / 2, width, height)
                    top += heights[child] + gap_y
                    pending.append(child)

        def transform_rect(rect: Rect) -> Rect:
            return Rect(rect.y, rect.x, rect.height, rect.width) if down else rect

        result = LayoutResult(nodes={key: transform_rect(rect) for key, rect in logical.items()})
        result.branches = route_branches(document, result.nodes)

        # 图片已计入节点测量尺寸；父子曲线位于控制点凸包内。合并这些几何范围，
        # 保证折叠、聚焦或删节点后边界重新计算，不继承上一次布局的空白范围。
        bounds = result.nodes[root]
        for rect in result.nodes.values():
            bounds = bounds.union(rect)
        for curve in result.branches.values():
            if isinstance(curve, BranchPath):
                bounds = bounds.union(curve.bounds)
            else:
                xs, ys = zip(*curve.points, strict=True)
                bounds = bounds.union(Rect(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)))
        # 40 单位是绘制/导出的留白；按窗口尺寸换算的一屏操作余量由视图单独维护。
        result.bounds = bounds.padded(40)
        return result
