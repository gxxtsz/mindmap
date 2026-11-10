"""使用只有文本插件的通用宿主，不通过产品应用间接安装导图业务。"""

import pytest
from workbench_plugin_text import manifest
from workbench_runtime import WorkbenchRuntimeWindow


@pytest.fixture
def window(qtbot, tmp_path):
    """创建隔离配置的真实窗口；测试后释放后台对象和全部插件订阅。"""
    result = WorkbenchRuntimeWindow(storage_dir=tmp_path / "配置", restore=False, manifests=[manifest])
    qtbot.addWidget(result, before_close_func=lambda item: item.dispose())
    result.show()
    yield result
    result.dispose()


@pytest.fixture
def workspace(window):
    """沿用迁入用例的夹具名称，确保同一测试始终复用同一个窗口。"""
    return window
