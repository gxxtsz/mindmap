"""产品工厂仅装配公开服务，插件配置不会重新变成窗口专属接口。"""

from importlib.metadata import PackageNotFoundError, distribution, entry_points

import pytest
from mindmap_formats import create_default_format_registry
from mindmap_layouts import create_default_layout_registry
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


def test_only_new_product_is_installed_and_exposes_a_command():
    with pytest.raises(PackageNotFoundError):
        distribution("mindmap-workbench")
    commands = {item.name for item in entry_points(group="gui_scripts")}
    assert "personal-workbench" in commands and "mindmap-workbench" not in commands
