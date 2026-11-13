"""以思维导图为唯一主体的产品入口；软件架构等能力由节点页面注册表提供。"""

from pathlib import Path

from mindmap_application import create_application
from mindmap_desktop_runtime import DesktopConfig, DesktopRuntimeWindow, run_desktop
from PySide6.QtCore import QLibraryInfo, QStandardPaths, QTranslator
from PySide6.QtWidgets import QApplication

from .migration import migrate_legacy

PRODUCT_CONFIG = DesktopConfig(application_id="mindmap", title="思维导图", icon="mindmap")


def default_storage_dir() -> Path:
    """返回思维导图的本机配置目录；历史子目录仅由迁移层在首次启动时读取。"""
    root = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericConfigLocation)
    return Path(root) / "mindmap"


def build_window(*, storage_dir=None, theme=None, restore=True, options=None):
    """直接构造导图应用；options 可注入布局、格式及节点页面注册表。

    产品不接收业务清单或同级编辑器。扩展只通过 mindmap.node_pages 发现，
    页面业务内容由所属导图统一保存，本机配置仅保存窗口和导航现场。
    """
    directory = default_storage_dir() if storage_dir is None else storage_dir
    return DesktopRuntimeWindow(config=PRODUCT_CONFIG, storage_dir=directory,
                                theme=theme, restore=restore, application_factory=create_application,
                                migration=migrate_legacy, options=options)


def run(argv=None):
    def configure(parser):
        parser.add_argument("file", nargs="?", type=Path, help="启动时打开的文件")

    def builder(options):
        app = QApplication.instance()
        translator = QTranslator(app)
        translator.load("qtbase_zh_CN", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
        app.installTranslator(translator)
        app.setApplicationName(PRODUCT_CONFIG.title)
        window = build_window(storage_dir=options.state_dir, theme=options.theme, restore=not options.no_restore)
        window._translator = translator
        if options.file:
            window.call(lambda: window.open_path(options.file))
        return window

    return run_desktop(builder, argv, description="思维导图", configure_parser=configure)
