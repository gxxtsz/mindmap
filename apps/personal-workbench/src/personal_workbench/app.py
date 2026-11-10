"""个人工作台产品入口；配置迁移与插件选项在此装配，窗口能力归通用运行时。"""

from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QStandardPaths, QTranslator
from PySide6.QtWidgets import QApplication
from workbench_runtime import WorkbenchConfig, WorkbenchRuntimeWindow, run_desktop

from .migration import migrate_legacy, upgrade_profile

# 产品的历史应用标识和目录必须保持稳定；代码包改名不能导致已有会话失联。
PRODUCT_CONFIG = WorkbenchConfig(application_id="mindmap-workbench", title="个人工作台", icon="mindmap")


def default_storage_dir() -> Path:
    """继续读取既有配置位置；仅产品兼容层知道目录中的历史名称。"""
    root = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericConfigLocation)
    return Path(root) / "mindmap" / "mindmap-workbench"


def build_window(*, storage_dir=None, theme=None, restore=True, manifests=None, plugin_options=None):
    """构造通用窗口；plugin_options 按插件 ID 隔离，由插件自行解释配置。

    例如导图的布局和格式注册表放入 plugin_options["mindmap"]，产品不理解
    这些业务对象，也不向窗口注入编辑器属性。历史文件只读导入，已停用功能的配置
    记录在恢复会话前由产品迁移回调清理。
    """
    directory = default_storage_dir() if storage_dir is None else storage_dir
    return WorkbenchRuntimeWindow(config=PRODUCT_CONFIG, storage_dir=directory,
                                   theme=theme, restore=restore, manifests=manifests,
                                   migration=migrate_legacy, plugin_options=plugin_options,
                                   profile_upgrade=upgrade_profile)


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

    return run_desktop(builder, argv, description="个人工作台", configure_parser=configure)
