"""验证通用节点页的失效数据和工厂错误处理。"""

import pytest
from mindmap_app.app import build_window
from mindmap_core.formats import load_document
from mindmap_core.node_pages import extension_page_id
from mindmap_qt_editor.node_pages import NodePageProvider, NodePageWidget, UnavailableNodePage
from PySide6.QtWidgets import QMenu


@pytest.fixture
def window(qtbot, tmp_path):
    result = build_window(storage_dir=tmp_path / "配置", restore=False)
    qtbot.addWidget(result, before_close_func=lambda item: item.dispose())
    result.resize(1200, 800)
    result.show()
    assert result.new_document(tmp_path / "主体.mindmap")
    return result


class ExamplePage(NodePageWidget):
    """供回归验证注册表与图页可以各自打开。"""

    def __init__(self, context, parent):
        super().__init__(parent)
        self.context = context


def open_example(view):
    if view.pages.registry.get("example") is None:
        view.pages.registry.register(NodePageProvider("example", "示例", ExamplePage, dict))
    assert view.pages.open_page(view.engine.root_id, "example", create=True)
    assert isinstance(view.pages._page, ExamplePage)
    return view.pages._page


def test_unknown_extension_is_preserved_and_recent_page_can_reopen(window):
    view = window.documents.active
    view.engine.set_extension_page(view.engine.root_id, "future", {"内容": "保留"}, version=77)
    view.pages.open_page(view.engine.root_id, "future")
    assert isinstance(view.pages._page, UnavailableNodePage)
    key = extension_page_id(view.engine.root_id, "future")
    assert (key, None) in view.document.recent_pages.keys
    view.pages.close_page()
    view._navigate_recent_page(key, None)
    assert view.pages.active
    assert view.document.save()
    assert load_document(view.document.path).root.extension_pages["future"]["version"] == 77


def test_closed_page_callbacks_stay_invalid_when_same_page_reopens(window):
    view = window.documents.active
    context = open_example(view).context
    view.pages.close_page()
    open_example(view)
    with pytest.raises(ValueError, match="关闭"):
        context.write({})
    with pytest.raises(ValueError, match="关闭"):
        context.read()


def test_extension_menu_does_not_mutate_or_export_hidden_canvas(window):
    open_example(window.documents.active)
    controller = window.application
    edit, export = QMenu(), QMenu()
    controller.populate_edit_menu(edit)
    controller.populate_export_menu(export)
    assert [action.text() for action in edit.actions()] == ["返回导图"]
    assert not export.actions()
    assert not controller.settings_action.isEnabled()
    assert all(not button.isVisible() for button in controller._status_controls.buttons.values())
    edit.deleteLater()
    export.deleteLater()


def test_unsupported_business_version_replaces_open_page_without_downgrade(window):
    view = window.documents.active
    old = open_example(view).context
    view.engine.set_extension_page(view.engine.root_id, "example", {"未来内容": True}, version=99)
    assert isinstance(view.pages._page, UnavailableNodePage)
    with pytest.raises(ValueError):
        old.write({})
    assert view.engine.extension_page(view.engine.root_id, "example")["version"] == 99
    view.engine.undo()
    assert isinstance(view.pages._page, ExamplePage)


def test_extension_factory_failure_keeps_data_and_other_pages_usable(window):
    def broken(context, parent):
        raise RuntimeError("测试初始化失败")

    view = window.documents.active
    view.pages.registry.register(NodePageProvider("broken", "故障示例", broken, lambda: {"保留": 1}))
    assert view.pages.open_page(view.engine.root_id, "broken", create=True)
    assert isinstance(view.pages._page, UnavailableNodePage)
    assert "测试初始化失败" in window.operation_log.plain_text()
    assert view.document.save()
    view.pages.close_page()
    view.canvas.open_node_graph(create=True)
    assert view.canvas.current_page_kind == "graph"
    assert view.engine.extension_page(view.engine.root_id, "broken")["data"] == {"保留": 1}
