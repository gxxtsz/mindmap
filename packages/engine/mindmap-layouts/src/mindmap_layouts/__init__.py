"""内置布局实现；第三方算法直接实现 mindmap-core 中的协议即可。"""

from mindmap_core import LayoutRegistry

from .connectors import route_branch, route_branches
from .tree import TreeLayout


def create_default_layout_registry() -> LayoutRegistry:
    """每次创建独立注册表和算法实例，宿主可继续显式注册自己的算法。"""
    registry = LayoutRegistry()
    # 内置实现只读取文档与测量表；第三方 register() 仍默认深拷贝隔离其副作用。
    registry.register(TreeLayout(), copy_inputs=False)
    return registry


__all__ = ["TreeLayout", "create_default_layout_registry", "route_branch", "route_branches"]
