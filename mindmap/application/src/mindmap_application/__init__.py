"""导图应用的唯一装配入口，由产品直接构造并持有。"""

from mindmap_desktop_api import ApplicationContext, MindMapApplication


def create_application(context: ApplicationContext) -> MindMapApplication:
    """创建导图控制器。延迟导入界面实现，让产品能够先建立桌面服务和恢复屏障。"""
    from .controller import MindMapController

    return MindMapController(context)
