"""所有分支样式的布局不变量、共享描边和箭头方向验收。"""

import math

import pytest
from mindmap_core import BranchPath, BranchStyle, Curve, LayoutConfig, MindMapEngine, Rect
from mindmap_layouts import create_default_layout_registry, route_branch


@pytest.mark.parametrize("kind", list(BranchStyle))
@pytest.mark.parametrize("direction", ["right", "left", "both", "down"])
@pytest.mark.parametrize("compact", [False, True])
def test_all_styles_directions_and_bounds(kind, direction, compact):
    engine = MindMapEngine()
    engine.set_layout(LayoutConfig(direction=direction, compact=compact))
    for index in range(3):
        child = engine.add_child(engine.root_id, f"分支{index}")
        engine.add_child(child, "下一层")
    engine.set_branch_style([engine.root_id], kind)
    registry = create_default_layout_registry()
    document = engine.document
    sizes = {key: (45 + index * 19, 24 + index * 7) for index, key in enumerate(document.nodes)}
    result = registry.layout(document, sizes)
    assert result == registry.layout(document, sizes)
    assert document == engine.document
    assert len(result.branches) == len(result.nodes) - 1
    parents = document.parents()
    for key, branch in result.branches.items():
        assert isinstance(branch, BranchPath)
        for point, rect in ((branch.start, result.nodes[parents[key]]), (branch.end, result.nodes[key])):
            x, y = point
            assert rect.x <= x <= rect.right and rect.y <= y <= rect.bottom
            assert x in (rect.x, rect.right) or y in (rect.y, rect.bottom)
        bounds = branch.bounds
        assert result.bounds.x <= bounds.x and result.bounds.right >= bounds.right
        assert result.bounds.y <= bounds.y and result.bounds.bottom >= bounds.bottom
        assert bool(branch.arrow) == (kind in (BranchStyle.ARROW, BranchStyle.ARROW2))


@pytest.mark.parametrize("kind", list(BranchStyle))
@pytest.mark.parametrize("advance", [0, 0.00001, 0.5, 64])
@pytest.mark.parametrize("offset", [-200, 0, 200])
@pytest.mark.parametrize("direction", ["right", "left", "down"])
def test_short_and_degenerate_branches(kind, advance, offset, direction):
    parent = Rect(0, -10, 20, 20)
    if direction == "right":
        child, start, end = Rect(20 + advance, offset - 5, 10, 10), (20, 0), (20 + advance, offset)
    elif direction == "left":
        child, start, end = Rect(-advance - 10, offset - 5, 10, 10), (0, 0), (-advance, offset)
    else:
        child = Rect(5 + offset, 10 + advance, 10, 10)
        start, end = (10, 10), (10 + offset, 10 + advance)
    branch = route_branch(parent, child, kind, direction=direction)
    assert branch.start == start and branch.end == pytest.approx(end)
    for command in branch.commands:
        assert all(math.isfinite(v) for point in command.points for v in point)
    if branch.arrow:
        assert branch.arrow[0] == branch.end
        # 三角形底边中心指向尖端的向量应平行于最后一段的切向；镜像、转置同样成立。
        current, tangent = branch.start, (1.0, 0.0)
        for command in branch.commands:
            last = command.points[-1]
            if command.op in ("L", "C"):
                origin = command.points[-2] if command.op == "C" else current
                tangent = last[0] - origin[0], last[1] - origin[1]
            current = last
        arrow_axis = tuple(branch.end[i] - (branch.arrow[1][i] + branch.arrow[2][i]) / 2 for i in (0, 1))
        assert abs(tangent[0] * arrow_axis[1] - tangent[1] * arrow_axis[0]) < 1e-7
        if math.hypot(*tangent) > 1e-8:
            assert sum(a * b for a, b in zip(tangent, arrow_axis, strict=True)) > 0


def test_line_does_not_replace_default_and_level_is_local():
    engine = MindMapEngine()
    child = engine.add_child(engine.root_id)
    leaf = engine.add_child(child)
    registry = create_default_layout_registry()
    before = registry.layout(engine.document)
    assert all(isinstance(branch, Curve) for branch in before.branches.values())
    engine.set_branch_style([engine.root_id], "line", scope="level")
    result = registry.layout(engine.document)
    assert isinstance(result.branches[child], BranchPath) and isinstance(result.branches[leaf], Curve)
    engine.set_branch_style([engine.root_id], None)
    assert registry.layout(engine.document) == before


@pytest.mark.parametrize(
    "kind", [BranchStyle.ELBOW, BranchStyle.ROUND, BranchStyle.CENTRE, BranchStyle.ARROW]
)
@pytest.mark.parametrize("direction", ["right", "left", "down"])
def test_same_paint_main_trunk_has_no_overlapping_segments(kind, direction):
    engine = MindMapEngine()
    engine.set_layout(LayoutConfig(direction=direction))
    first = engine.add_child(engine.root_id)
    for _ in range(5):
        engine.add_child(first)
    engine.set_branch_style([first], kind)
    result = create_default_layout_registry().layout(engine.document)
    segments = []
    for branch in result.branches.values():
        if not isinstance(branch, BranchPath):
            continue
        current = branch.start
        for command in branch.commands:
            if command.op == "L":
                end = command.points[0]
                if current[0] == end[0] and current[1] != end[1]:
                    segments.append((1, current[0], *sorted((current[1], end[1]))))
                elif current[1] == end[1] and current[0] != end[0]:
                    segments.append((0, current[1], *sorted((current[0], end[0]))))
            current = command.points[-1]
    assert segments
    for index, (axis, x, a, b) in enumerate(segments):
        assert all(
            axis != other_axis or x != xx or min(b, d) - max(a, c) <= 1e-8
            for other_axis, xx, c, d in segments[index + 1 :]
        )


def test_fold_focus_and_expand_keep_styles_and_document():
    engine = MindMapEngine()
    first = engine.add_child(engine.root_id)
    child = engine.add_child(first)
    engine.set_branch_style([first], "arrow2")
    engine.set_expanded(first, False)
    registry = create_default_layout_registry()
    document = engine.document
    assert child not in registry.layout(document).branches
    assert isinstance(registry.layout(document, expand_all=True).branches[child], BranchPath)
    focused = registry.layout(document, focus_id=first, expand_all=True)
    assert set(focused.branches) == {child}
    assert document == engine.document
