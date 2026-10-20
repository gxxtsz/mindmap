"""独立工作台入口；引擎和 Qt 组件无需安装 Harbor。"""

def main(argv=None):
    """延迟加载应用适配层，以命令行或 GUI 入口启动单文档工作台。"""
    from .harbor_adapter import run

    return run(argv)
