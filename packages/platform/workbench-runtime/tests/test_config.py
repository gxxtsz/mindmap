"""产品配置隔离：通用宿主复用时不继承个人工作台名称和历史目录。"""

import pytest
from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QLabel
from workbench_plugin_text import manifest
from workbench_runtime import WorkbenchConfig, WorkbenchRuntimeWindow


def test_custom_product_config_controls_window_welcome_and_document_title(qtbot, tmp_path):
    """新产品使用同一运行时，空白页和编辑后的窗口标题都须来自配置。"""
    config = WorkbenchConfig(application_id="example-workbench", title="研究工作台", icon="document")
    window = WorkbenchRuntimeWindow(config=config, storage_dir=tmp_path, manifests=[manifest], restore=False)
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    window.show()
    assert window.manager.application_id == config.application_id
    assert window.windowTitle() == "研究工作台"
    assert any(label.text() == "研究工作台" for label in window.findChildren(QLabel))
    assert window.new_document(provider_id="text.editor")
    window.documents.active.widget.insertPlainText("正文")
    assert window.windowTitle().endswith(" — 研究工作台")
    assert window.spec.icon == "document"


def test_default_runtime_directory_uses_only_declared_application_id(monkeypatch, tmp_path):
    monkeypatch.setattr(QStandardPaths, "writableLocation", lambda location: str(tmp_path))
    assert WorkbenchConfig(application_id="another-product").default_storage_dir() == tmp_path / "another-product"


@pytest.mark.parametrize("identifier", ["", ".", "..", "../outside", "nested/path", "nested\\path", "C:outside"])
def test_product_identifier_cannot_escape_default_storage(identifier):
    with pytest.raises(ValueError):
        WorkbenchConfig(application_id=identifier)
