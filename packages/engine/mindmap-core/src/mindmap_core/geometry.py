"""布局算法和渲染后端共享的几何结果，不依赖任何具体布局。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Point = tuple[float, float]


@dataclass(frozen=True)
class Rect:
    """不可变内容矩形，使用与渲染后端无关的逻辑坐标和尺寸。"""

    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def center(self) -> Point:
        return self.x + self.width / 2, self.y + self.height / 2

    def union(self, other: Rect) -> Rect:
        x, y = min(self.x, other.x), min(self.y, other.y)
        return Rect(x, y, max(self.right, other.right) - x, max(self.bottom, other.bottom) - y)

    def padded(self, amount: float) -> Rect:
        return Rect(self.x - amount, self.y - amount, self.width + 2 * amount, self.height + 2 * amount)

    def intersects(self, other: Rect) -> bool:
        return (
            self.x < other.right and other.x < self.right and self.y < other.bottom and other.y < self.bottom
        )


@dataclass(frozen=True)
class Curve:
    """三次贝塞尔曲线，points 顺序为端点、控制点、控制点、端点。"""

    points: tuple[Point, Point, Point, Point]
    color: str


@dataclass(frozen=True)
class PathCommand:
    """不可变路径指令：M/L 各一个点、C 三个点、Z 无点；坐标均为逻辑坐标。"""

    op: Literal["M", "L", "C", "Z"]
    points: tuple[Point, ...] = ()


@dataclass(frozen=True)
class BranchPath:
    """复合分支路径及独立的填充箭头，不绑定具体绘图框架。

    start/end 保存父子边界上的逻辑锚点。共享主干由同组第一条边拥有，因此某条
    边的实际 M 指令可以从主干接点开始，而不必重复绘制整个父节点出线。
    箭头三点按尖端、底角、底角排列，普通分支为 None。
    """

    start: Point
    end: Point
    commands: tuple[PathCommand, ...]
    color: str
    width: float = 1.8
    arrow: tuple[Point, Point, Point] | None = None

    @property
    def bounds(self) -> Rect:
        """控制点凸包覆盖曲线，合并箭头及描边连接，保守避免导出裁剪。

        新分支使用斜接连接，急角的外缘可能超过半线宽；按照渲染约定的斜接上限
        两倍线宽留边，也覆盖圆端帽。这里不求精确曲线极值，保持核心无 Qt 依赖。
        """
        points = [self.start, self.end, *(p for command in self.commands for p in command.points)]
        if self.arrow is not None:
            points.extend(self.arrow)
        xs, ys = zip(*points, strict=True)
        return Rect(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)).padded(self.width * 2)


@dataclass
class LayoutResult:
    """可见节点、普通父子分支及内容边界；不包含视图平移所需的操作余量。"""

    nodes: dict[str, Rect] = field(default_factory=dict)
    branches: dict[str, Curve | BranchPath] = field(default_factory=dict)
    bounds: Rect = Rect(0, 0, 1, 1)
