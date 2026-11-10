"""内置布局的可选增量能力；具体实现与优化准入统一由布局包维护。"""

from collections.abc import Mapping

from mindmap_core import LayoutRegistry, MindMapDocument, MindMapForestView, Point

from .incremental_result import IncrementalTreeLayout
from .tree import TreeLayout

# 保留装配时的标准入口，运行中修改类方法也必须走宿主的完整调度。
_REGISTRY_METHODS = {name: getattr(LayoutRegistry, name) for name in ("get", "info", "validate_config", "layout")}
_TREE_LAYOUT = TreeLayout.layout
_TREE_VALIDATE = TreeLayout.validate_config


def supports_incremental_layout(registry: LayoutRegistry, algorithm_id: str) -> bool:
    """查询是否可以使用本包受控的增量服务，不执行布局或改写注册表。

    只有标准注册表和未替换入口的内置树算法可走该路径。注册表子类、算法子类、
    实例级替换方法均保留完整调度，避免跳过宿主增加的布局处理和校验。能力判断
    留在实现提供者内部，Qt 调用方无需判断具体类型、方法归属或实例私有状态。
    未注册算法沿用注册表的中文异常，不静默选择其他算法。
    """
    if type(registry) is not LayoutRegistry:
        return False
    for name, method in _REGISTRY_METHODS.items():
        if getattr(getattr(registry, name), "__func__", None) is not method:
            return False
    algorithm = registry.get(algorithm_id)
    return (type(algorithm) is TreeLayout
            and getattr(algorithm.layout, "__func__", None) is _TREE_LAYOUT
            and getattr(algorithm.validate_config, "__func__", None) is _TREE_VALIDATE)


def create_incremental_layout(
    registry: LayoutRegistry,
    document: MindMapDocument,
    measurements: Mapping[str, Point],
) -> IncrementalTreeLayout | None:
    """为已校验单根文档建立增量服务；多根灵感区及不支持的注入实现返回 None。

    document 必须来自引擎已提交的文档或正式校验后的快照，measurements 包含全部
    节点尺寸。服务只读取输入，后续通过 update() 消费提交差量；调用方负责文档
    重置、全局配置变化时重新创建。返回 None 时必须继续使用 registry.layout()，
    不能自行构造内置实现绕过宿主。缺失算法或非法配置沿用注册表异常语义。
    """
    if isinstance(document, MindMapForestView) or not supports_incremental_layout(registry, document.layout.algorithm_id):
        return None
    registry.validate_config(document.layout)
    return IncrementalTreeLayout(document, measurements)
