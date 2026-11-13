"""软件架构节点详情页的类型声明及工厂，由导图页面容器创建与释放。"""

from mindmap_qt_editor.node_pages import NodePageProvider


def create_page(context, parent):
    """进入节点页面时才加载画布，发现扩展不会创建 Qt 控件或文档。"""
    from .page import SoftwareStructurePage

    return SoftwareStructurePage(context, parent)


def create_data() -> dict:
    """新页面从空模块开始；用户可从页内导入已有软件结构文件。"""
    return {"schema_version": 1, "modules": [], "module_dependencies": [],
            "module_apis": [], "hidden_edge_module_ids": []}


provider = NodePageProvider("software-structure", "软件架构", create_page, create_data)
