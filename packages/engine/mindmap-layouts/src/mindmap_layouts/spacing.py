"""内置树布局的间距契约；完整布局、增量几何和界面共用同一解析入口。"""

from math import isfinite

from mindmap_core import LayoutConfig, MindMapError


def resolve_tree_spacing(config: LayoutConfig) -> tuple[float, float]:
    """返回父子、同级子树间距，单位为与缩放无关的导图逻辑像素。

    options 只接受 level_gap（24～500）和 sibling_gap（4～500）的有限数值，
    拒绝布尔值、未知键及其他算法。缺省值继续由 compact 决定，保证既有文件
    的几何不变；显式参数逐项覆盖缺省值，不修改传入配置或补写默认参数。
    两个返回值沿树的生长轴、横轴定义，向下布局由调用方转置为屏幕坐标。
    """
    config.validate()
    if config.algorithm_id != "tree":
        raise MindMapError("树布局配置的算法标识无效")
    if config.options.keys() - {"level_gap", "sibling_gap"}:
        raise MindMapError("树布局包含不支持的额外布局选项")
    defaults = (32.0, 10.0) if config.compact else (64.0, 22.0)
    values = []
    for key, label, minimum, default in (
        ("level_gap", "父子间距", 24, defaults[0]),
        ("sibling_gap", "同级间距", 4, defaults[1]),
    ):
        value = config.options.get(key, default)
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not minimum <= value <= 500 or not isfinite(value)):
            raise MindMapError(f"{label}必须为 {minimum}～500 的有限数值")
        values.append(float(value))
    return values[0], values[1]
