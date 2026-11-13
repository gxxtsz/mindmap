"""产品工厂直接装配导图主体，通过应用选项注入能力注册表。"""

from importlib.metadata import distribution

from mindmap_app.app import build_window
from mindmap_core.formats import create_default_format_registry
from mindmap_core.layouts import create_default_layout_registry
from mindmap_desktop_runtime import DesktopRuntimeWindow


def test_factory_passes_registries_through_application_options(qtbot, tmp_path):
    layout = create_default_layout_registry()
    formats = create_default_format_registry()
    window = build_window(storage_dir=tmp_path / "配置", restore=False, options={"layout_registry": layout, "format_registry": formats})
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    assert type(window) is DesktopRuntimeWindow
    assert window.new_document(tmp_path / "注入.mindmap")
    view = window.documents.active
    assert view.canvas.layout_registry is layout and view.canvas.format_registry is formats
    assert not hasattr(window, "canvas") and not hasattr(window, "sessions")


def test_product_exposes_its_gui_command():
    """验证产品发行包声明的命令及其调用目标。"""
    commands = distribution("mindmap-app").entry_points.select(group="gui_scripts")
    assert {item.name: item.value for item in commands} == {"mindmap": "mindmap_app:main"}
