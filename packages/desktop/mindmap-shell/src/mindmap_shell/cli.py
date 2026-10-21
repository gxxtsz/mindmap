"""桌面应用和模板共用的启动辅助函数；嵌入库时无需使用此模块。"""

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from .window import WorkbenchWindow


def run_desktop(builder: Callable[[argparse.Namespace], WorkbenchWindow], argv=None, *, description="原生桌面应用",
                configure_parser: Callable[[argparse.ArgumentParser], None] | None = None):
    """解析通用启动参数并运行业务窗口；由调用方拥有业务数据和退出决策。

    configure_parser 可在解析前向解析器追加业务参数；不得覆盖框架已有参数。
    builder 接收完整 Namespace，返回 WorkbenchWindow。参数定义冲突、非法
    参数继续由 argparse 报错；截图输出使用传入路径，退出时释放框架会话。
    """
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--theme", choices=["light", "dark"], help="选择深浅主题")
    parser.add_argument("--state-dir", type=Path, help="使用独立配置目录")
    parser.add_argument("--no-restore", action="store_true", help="忽略上次会话")
    parser.add_argument("--compact", action="store_true", help="启用紧凑模式")
    parser.add_argument("--size", default="1440x900", help="窗口逻辑尺寸，例如 1440x900")
    parser.add_argument("--screenshot", type=Path, help="保存实际窗口图像并退出")
    if configure_parser is not None:
        configure_parser(parser)
    options = parser.parse_args(argv)
    try:
        width, height = [int(value) for value in options.size.lower().split("x")]
        if not (860 <= width <= 8000 and 540 <= height <= 8000):
            raise ValueError
    except ValueError:
        parser.error("尺寸必须介于 860x540 与 8000x8000 之间")
    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = builder(options)
    if options.compact:
        window.manager.set_option("compact", True)
    window.resize(width, height)
    window.show()
    failed = []
    if options.screenshot:
        def capture():
            try:
                options.screenshot.parent.mkdir(parents=True, exist_ok=True)
                if not window.grab().save(str(options.screenshot)):
                    raise OSError("截图保存失败")
                options.screenshot.with_suffix(".json").write_text(json.dumps({
                    "application_id": window.spec.application_id, "platform": app.platformName(),
                    "theme": window.manager.theme.name, "dpr": window.devicePixelRatioF(),
                    "logical_size": [window.width(), window.height()],
                }, ensure_ascii=False, indent=2), "utf-8")
            except OSError as error:
                failed.append(str(error))
            app.quit()
        QTimer.singleShot(700, capture)
    try:
        result = app.exec()
    finally:
        window.manager.shutdown()
        window.close()
        # 离屏平台没有外部剪贴板服务；必须在 QApplication 和 Python 绑定仍有效时
        # 释放自定义 MIME，避免退出时的原生访问冲突。原生平台保留系统剪贴板内容。
        if app.platformName() == "offscreen":
            app.clipboard().clear()
    if failed:
        print(failed[0], file=sys.stderr)
        return 1
    return result
