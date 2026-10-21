"""独立工作台入口；引擎和 Qt 组件无需安装桌面外壳。"""

def main(argv=None):
    """延迟加载应用适配层，以命令行或 GUI 入口启动多导图工作台。"""
    from multiprocessing import freeze_support

    freeze_support()
    from .shell_adapter import run

    return run(argv)
