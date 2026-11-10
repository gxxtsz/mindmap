"""产品工厂仅装配公开服务，插件配置不会重新变成窗口专属接口。"""

from importlib.metadata import distribution

from mindmap_core.formats import create_default_format_registry
from mindmap_core.layouts import create_default_layout_registry
from personal_workbench.app import build_window
from workbench_runtime import WorkbenchRuntimeWindow


def test_factory_passes_registries_through_plugin_options(qtbot, tmp_path):
    layout = create_default_layout_registry()
    formats = create_default_format_registry()
    window = build_window(storage_dir=tmp_path / "配置", restore=False, plugin_options={
        "mindmap": {"layout_registry": layout, "format_registry": formats},
    })
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    assert type(window) is WorkbenchRuntimeWindow
    assert window.new_document(tmp_path / "注入.mindmap")
    view = window.documents.active
    assert view.canvas.layout_registry is layout and view.canvas.format_registry is formats
    assert not hasattr(window, "canvas") and not hasattr(window, "sessions")


def test_product_exposes_its_gui_command():
    """验证产品发行包声明的命令及其调用目标。"""
    commands = distribution("personal-workbench").entry_points.select(group="gui_scripts")
    assert {item.name: item.value for item in commands} == {"personal-workbench": "personal_workbench:main"}
