"""从产品入口验证节点详情、原文件导入、多标签共享与会话恢复。"""

import json

import pytest
from mindmap_app.app import build_window
from mindmap_core.formats import load_document
from mindmap_core.node_pages import extension_page_id
from mindmap_extension_software_structure.page import SoftwareStructurePage
from mindmap_qt_editor.node_pages import NodePageRegistry, UnavailableNodePage
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMenu


@pytest.fixture
def window(qtbot, tmp_path):
    result = build_window(storage_dir=tmp_path / "配置", restore=False)
    qtbot.addWidget(result, before_close_func=lambda item: item.dispose())
    result.resize(1200, 800)
    result.show()
    assert result.new_document(tmp_path / "主体.mindmap")
    return result


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "来源.software-structure"
    path.write_text(json.dumps({
        "schema_version": 1,
        "modules": [{"id": "a", "name": "入口"}, {"id": "b", "name": "服务"}],
        "module_dependencies": [{"source": "a", "target": "b"}],
        "module_apis": [{"module_id": "a", "apis": [{"name": "启动", "description": "启动服务"}]}],
        "额外数据": {"保留": True},
    }, ensure_ascii=False), encoding="utf-8")
    return path


def open_structure(view):
    """从当前根节点进入实际扩展，确保没有被故障占位页悄悄替换。"""
    assert view.pages.open_page(view.engine.root_id, "software-structure", create=True)
    page = view.pages._page
    assert isinstance(page, SoftwareStructurePage)
    return page


def test_product_has_one_document_type_and_discovers_node_extensions(window):
    assert window.application is not None
    assert not hasattr(window, "providers") and not hasattr(window, "plugins")
    assert window.application.page_registry.get("software-structure")
    menu = QMenu()
    window.documents.active.pages.populate_menu(menu)
    assert any(action.text() == "添加软件架构" for action in menu.actions())
    menu.deleteLater()


def test_import_embeds_data_without_modifying_source(window, source):
    view = window.documents.active
    original = source.read_bytes()
    page = open_structure(view)
    page.import_file(source)
    assert len(window.documents.views) == 1
    assert view.document.save()
    document = load_document(view.document.path)
    data = document.root.extension_pages["software-structure"]["data"]
    assert len(data["modules"]) == 2 and data["额外数据"] == {"保留": True}
    assert source.read_bytes() == original
    with pytest.raises(ValueError, match="只能打开"):
        window.open_path(source)
    assert window.documents.active is view


def test_navigation_returns_to_same_canvas_and_restores_extension_viewport(window, source, qtbot):
    view = window.documents.active
    canvas = view.canvas
    qtbot.wait(40)
    before = canvas.view.transform()
    page = open_structure(view)
    page.import_file(source)
    qtbot.wait(40)
    assert window.status.currentMessage() and not window.status_label.isVisible()
    page.widget.reset_zoom()
    state = page.capture_state()
    assert view.pages.navigation.full_path.endswith("软件架构")
    view.pages.close_page()
    qtbot.wait(20)
    assert not window.status_label.isVisible()
    assert view.widget.currentWidget() is canvas and canvas.view.transform() == before
    open_structure(view)
    qtbot.wait(30)
    assert view.pages._page.capture_state()["viewport"]["zoom"] == state["viewport"]["zoom"]
    assert not window.status_label.isVisible()
    window.status.clearMessage()
    qtbot.wait(20)
    assert window.status_label.isVisible() and "节点扩展详情" in window.status_label.text()


def test_filter_shares_undo_save_and_duplicate_tabs(window, source):
    first = window.documents.active
    page = open_structure(first)
    page.import_file(source)
    assert first.duplicate()
    second = window.documents.active
    assert second is not first and second.document is first.document
    assert isinstance(second.pages._page, SoftwareStructurePage)
    assert second.pages._page.document.set_module_edges_hidden("a", True)
    assert page.document.content.hidden_edge_module_ids == frozenset({"a"})
    first.engine.undo()
    assert not page.document.content.hidden_edge_module_ids
    assert not second.pages._page.document.content.hidden_edge_module_ids
    first.engine.redo()
    assert first.document.save()
    loaded = load_document(first.document.path)
    assert loaded.root.extension_pages["software-structure"]["data"]["hidden_edge_module_ids"] == ["a"]


def test_read_only_browses_but_rejects_mutations(window, source):
    view = window.documents.active
    page = open_structure(view)
    page.import_file(source)
    view.canvas.set_editable(False)
    assert not page.import_action.isEnabled() and not page.filter_panel.isEnabled()
    before = view.engine.document
    assert not page.document.set_all_module_edges_hidden(True)
    with pytest.raises(ValueError, match="只读"):
        page.import_file(source)
    with pytest.raises(ValueError, match="只读"):
        page.context.write({})
    assert view.engine.document == before
    view.pages.close_page()
    assert view.pages.open_page(view.engine.root_id, "software-structure")


def test_owner_deletion_returns_to_map_and_undo_restores_content(window):
    view = window.documents.active
    node = view.engine.add_child(view.engine.root_id, "架构入口")
    view.pages.open_page(node, "software-structure", create=True)
    context = view.pages._page.context
    view.engine.remove_nodes([node])
    assert not view.pages.active
    with pytest.raises(ValueError, match="关闭"):
        context.write({})
    view.engine.undo()
    assert view.engine.extension_page(node, "software-structure") is not None


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


def test_restart_restores_same_node_page_and_data(window, source, qtbot, tmp_path):
    view = window.documents.active
    page = open_structure(view)
    page.import_file(source)
    assert window.close()
    reopened = build_window(storage_dir=tmp_path / "配置")
    qtbot.addWidget(reopened, before_close_func=lambda item: item.dispose())
    reopened.show()
    restored = reopened.documents.active
    assert restored.pages.active
    assert isinstance(restored.pages._page, SoftwareStructurePage)
    assert len(restored.pages._page.document.content.modules) == 2


def test_missing_extension_keeps_document_open(qtbot, tmp_path):
    window = build_window(storage_dir=tmp_path / "配置", restore=False,
                          options={"page_registry": NodePageRegistry()})
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    assert window.new_document(tmp_path / "未知扩展.mindmap")
    view = window.documents.active
    view.engine.set_extension_page(view.engine.root_id, "software-structure", {"modules": []})
    assert view.pages.open_page(view.engine.root_id, "software-structure")
    assert isinstance(view.pages._page, UnavailableNodePage)
    assert view.document.save()


def test_module_selection_and_filter_list_are_inside_the_page(window, source, qtbot):
    page = open_structure(window.documents.active)
    page.import_file(source)
    qtbot.wait(50)
    assert page.sidebar.width() >= 150 and page.sidebar.isVisible()
    item = page.widget._nodes["a"][0]
    point = page.widget.mapFromScene(item.sceneBoundingRect().center())
    qtbot.mouseClick(page.widget.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert page.widget.selected_module_id == "a"
    assert page.api_panel._selection == (page.id, "a")
    modules = page.filter_panel._module_list
    modules.item(0).setCheckState(Qt.CheckState.Checked)
    assert page.document.content.hidden_edge_module_ids == frozenset({"a"})
    assert page.widget.has_hidden_dependencies
    assert window.documents.active.document.recent_pages.entries[0].title.endswith(" · 软件架构")


def test_node_badge_opens_extension_and_undo_removes_badge(window, qtbot):
    view = window.documents.active
    node = view.engine.add_child(view.engine.root_id, "有专用详情的节点")
    view.engine.set_extension_page(node, "software-structure", {"schema_version": 1, "modules": []})
    view.canvas.view.fitInView(view.canvas.scene.itemsBoundingRect(), Qt.AspectRatioMode.KeepAspectRatio)
    qtbot.wait(30)
    control = view.canvas.node_items[node].extension_control
    assert control is not None and control.isVisible()
    point = view.canvas.view.mapFromScene(control.mapToScene(control.boundingRect().center()))
    qtbot.mouseClick(view.canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert isinstance(view.pages._page, SoftwareStructurePage)
    view.engine.undo()
    assert not view.pages.active
    assert not view.canvas.node_items[node].extension_control.isVisible()


def test_closed_page_callbacks_stay_invalid_when_same_page_reopens(window):
    view = window.documents.active
    context = open_structure(view).context
    view.pages.close_page()
    open_structure(view)
    with pytest.raises(ValueError, match="关闭"):
        context.write({"schema_version": 1, "modules": []})
    with pytest.raises(ValueError, match="关闭"):
        context.read()


def test_extension_menu_does_not_mutate_or_export_hidden_canvas(window):
    view = window.documents.active
    open_structure(view)
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
    old = open_structure(view).context
    view.engine.set_extension_page(view.engine.root_id, "software-structure", {"未来内容": True}, version=99)
    assert isinstance(view.pages._page, UnavailableNodePage)
    with pytest.raises(ValueError):
        old.write({"schema_version": 1, "modules": []})
    assert view.engine.extension_page(view.engine.root_id, "software-structure")["version"] == 99
    view.engine.undo()
    assert isinstance(view.pages._page, SoftwareStructurePage)


def test_invalid_import_retains_existing_node_content(window, source, tmp_path):
    view = window.documents.active
    page = open_structure(view)
    page.import_file(source)
    before = view.engine.document
    invalid = tmp_path / "错误依赖.software-structure"
    invalid.write_text('{"schema_version":1,"modules":[],"module_dependencies":[{"source":"a","target":"b"}]}',
                       encoding="utf-8")
    with pytest.raises(ValueError, match="未声明"):
        page.import_file(invalid)
    assert view.engine.document == before
    assert len(page.document.content.modules) == 2


def test_extension_factory_failure_keeps_data_and_other_pages_usable(window):
    from mindmap_qt_editor.node_pages import NodePageProvider

    def broken(context, parent):
        raise RuntimeError("测试初始化失败")

    view = window.documents.active
    view.pages.registry.register(NodePageProvider("broken", "故障示例", broken, lambda: {"保留": 1}))
    assert view.pages.open_page(view.engine.root_id, "broken", create=True)
    assert isinstance(view.pages._page, UnavailableNodePage)
    assert "测试初始化失败" in window.operation_log.plain_text()
    assert view.document.save()
    view.pages.close_page()
    assert isinstance(open_structure(view), SoftwareStructurePage)
    assert view.engine.extension_page(view.engine.root_id, "broken")["data"] == {"保留": 1}
