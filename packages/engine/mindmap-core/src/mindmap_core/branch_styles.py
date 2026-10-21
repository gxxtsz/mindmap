"""分支样式的稳定标识与文档操作；只使用标准库，不接触布局或 Qt。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .errors import MindMapError

if TYPE_CHECKING:
    from .models import MindMapDocument


class BranchStyle(StrEnum):
    """按选择器顺序定义语义标识；数值不依赖参考程序的内部枚举。"""

    LINE = "line"
    ARC = "arc"
    CURVE = "curve"
    ELBOW = "elbow"
    ROUND = "round"
    ARC2 = "arc2"
    ROUND2 = "round2"
    CENTRE = "centre"
    ARROW = "arrow"
    ARROW2 = "arrow2"
    CURVE_CONVEX = "curveconvex"

    @property
    def label(self) -> str:
        """返回界面和无障碍提示共用的中文名称。"""
        return {
            "line": "直线",
            "arc": "凹弧",
            "curve": "柔和曲线",
            "elbow": "正交折线",
            "round": "圆角正交",
            "arc2": "凹弧变体",
            "round2": "斜折线",
            "centre": "圆滑分叉",
            "arrow": "正交箭头",
            "arrow2": "弧线箭头",
            "curveconvex": "凸曲线",
        }[self.value]


def normalize_branch_style(kind: BranchStyle | str | None) -> BranchStyle | None:
    """校验调用参数；None 表示删除显式配置，恢复旧版原始曲线。"""
    if kind is None:
        return None
    try:
        return BranchStyle(kind)
    except (ValueError, TypeError) as exc:
        raise MindMapError(f"不支持的分支连接线样式：{kind}") from exc


def get_branch_style(style: Mapping[str, Any]) -> BranchStyle | None:
    """读取节点发出的连线设置；额外 JSON 字段原样保留，当前仅解释 kind。"""
    if "branch" not in style:
        return None
    branch = style["branch"]
    if not isinstance(branch, dict) or not isinstance(branch.get("kind"), str):
        raise MindMapError("节点分支样式必须是包含 kind 字符串的对象")
    return normalize_branch_style(branch["kind"])


def branch_style_targets(
    document: MindMapDocument,
    node_ids: Iterable[str],
    *,
    scope: str = "subtree",
) -> tuple[str, ...]:
    """取得同一套作用范围，供正式操作、混合状态提示和临时预览共用。"""
    if scope not in ("level", "subtree"):
        raise MindMapError("分支样式作用范围必须为 level 或 subtree")
    ids = list(node_ids)
    if any(not isinstance(key, str) or key not in document.nodes for key in ids):
        raise MindMapError("分支样式包含不存在的节点标识")
    selected = set(ids)
    targets = []
    # 一次显式遍历合并重叠选择，深树不依赖递归；整棵子树操作保持线性复杂度。
    pending = [(document.root_id, False)]
    while pending:
        key, inherited = pending.pop()
        active = key in selected or (scope == "subtree" and inherited)
        if active:
            targets.append(key)
        pending.extend((child, active) for child in reversed(document.nodes[key].children))
    return tuple(targets)


def apply_branch_style(
    document: MindMapDocument,
    node_ids: Iterable[str],
    kind: BranchStyle | str | None,
    *,
    scope: str = "subtree",
) -> tuple[str, ...]:
    """在独立文档或引擎事务内部设置发出的分支，先验证参数再统一修改。

    subtree 包含折叠节点及叶节点，后者供以后新增主题继续使用；level 无隐式继承。
    本函数不记录历史，正式文档应通过 MindMapEngine.set_branch_style 修改。
    """
    value = normalize_branch_style(kind)
    targets = branch_style_targets(document, node_ids, scope=scope)
    for key in targets:
        style = document.nodes[key].style
        if value is None:
            style.pop("branch", None)
        else:
            branch = dict(style.get("branch", {}))
            branch["kind"] = value.value
            style["branch"] = branch
    return targets
