"""思维导图应用的延迟启动入口。"""


def main(argv=None):
    from multiprocessing import freeze_support
    freeze_support()
    from .app import run
    return run(argv)
