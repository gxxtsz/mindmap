"""用实际 Qt 控件验收页面选择、跳转、跨页刷新与引用剪贴板。"""

import pytest
from mindmap_core import MindMapEngine
from mindmap_qt_editor.canvas import MindMapWidget
from mindmap_qt_editor.panels import OutlinePanel
from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLineEdit, QPushButton, QTreeWidget


@pytest.fixture
def make_canvas(qtbot):
    """为测试创建彼此独立的视图，结束时解除引擎订阅。"""
    canvases = []

    def create(engine):
        canvas = MindMapWidget(engine, toolbar=False)
        canvas.shared_views = True
        qtbot.addWidget(canvas)
        canvas.resize(1000, 650)
        canvas.show()
        QApplication.processEvents()
        canvases.append(canvas)
        return canvas

    yield create
    for canvas in canvases:
        canvas.dispose()


def test_search_filters_real_pages_and_same_path_choice_keeps_its_identity(make_canvas, qtbot):
    engine = MindMapEngine()
    source = engine.add_child(engine.root_id, "原文")
    owners = [engine.add_child(engine.root_id, "同名目标") for _ in range(2)]
    pages = [engine.create_submap(key) for key in owners]
    roots = [engine.page_document(page).root_id for page in pages]
    detail = engine.create_detail_page(owners[0])
    detail_root = engine.page_document(detail).root_id
    canvas = make_canvas(engine)
    canvas.select_nodes([source])
    action = canvas.action("set_page_reference")
    errors = []
    canvas.error_occurred.connect(errors.append)
    revision = engine.revision

    def interact(search_text, selected_root, *, reject=False):
        dialog = QApplication.activeModalWidget()
        try:
            search = dialog.findChild(QLineEdit, "page_reference_search")
            tree = dialog.findChild(QTreeWidget, "page_reference_targets")
            buttons = dialog.findChild(QDialogButtonBox)
            items = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
            role = Qt.ItemDataRole.UserRole
            assert {item.data(0, role) for item in items} == {*roots, detail_root}
            assert all("中心主题" in item.text(1) for item in items)
            assert items[0].text(1) != items[1].text(1)
            search.setText(search_text)
            assert not buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
            if selected_root is not None:
                item = next(item for item in items if item.data(0, role) == selected_root)
                assert not item.isHidden()
                tree.setCurrentItem(item)
                button = buttons.button(QDialogButtonBox.StandardButton.Cancel if reject
                                        else QDialogButtonBox.StandardButton.Ok)
                qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        finally:
            if dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, lambda: interact("没有匹配", None))
    action.trigger()
    QTimer.singleShot(0, lambda: interact("子导图", roots[0], reject=True))
    action.trigger()
    assert engine.revision == revision and engine.resolve_page_reference(source) is None
    QTimer.singleShot(0, lambda: interact("子导图 同名目标", roots[1]))
    action.trigger()
    assert engine.revision == revision + 1 and not errors
    assert engine.resolve_page_reference(source).page_id == pages[1]
    assert action.text() == "更换引用页面…" and not canvas.action("edit").isEnabled()
    canvas.begin_edit(source)
    assert canvas.editor is None
    QTimer.singleShot(0, lambda: interact("详情页 同名目标", detail_root))
    action.trigger()
    assert engine.resolve_page_reference(source).page_id == detail
    canvas.action("open_page_reference").trigger()
    assert canvas.current_page_id == detail and canvas.selected_node_ids() == [detail_root]
    canvas.return_to_parent_page()
    canvas.action("clear_page_reference").trigger()
    assert canvas.document_view.nodes[source].text == "原文" and canvas.action("edit").isEnabled()
    canvas.select_nodes(owners)
    assert not action.isEnabled()
    canvas.select_nodes([engine.root_id])
    assert not action.isVisible()


@pytest.mark.parametrize("target_kind", ["main", "submap", "detail"])
def test_reference_button_and_back_restore_source_view_in_readonly(make_canvas, qtbot, target_kind):
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "目标")
    page = engine.create_detail_page(owner) if target_kind == "detail" else engine.create_submap(owner)
    source_page, target_page = (page, None) if target_kind == "main" else (None, page)
    source = engine.add_child(engine.page_document(source_page).root_id, "来源")
    if target_kind == "detail":
        engine.create_submap(source)
    else:
        engine.add_child(source, "仍有折叠按钮")
    engine.create_detail_page(source)
    target = engine.page_document(target_page).root_id
    engine.set_page_reference(source, target)
    canvas = make_canvas(engine)
    canvas.navigate_to(source_page, source)
    QApplication.processEvents()
    canvas.view.zoom(1.3)
    canvas.view.update_navigation_rect(center=QPointF(100, 60))
    canvas.set_editable(False)
    engine.mark_saved()
    revision = engine.revision
    state = canvas.capture_page_view_state()
    item = canvas.node_items[source]
    button = item.reference_control
    assert button.can_activate() and canvas.action("open_page_reference").isEnabled()
    assert not canvas.action("set_page_reference").isEnabled()
    assert not canvas.action("clear_page_reference").isEnabled()
    assert not button.boundingRect().intersects(item.detail_page_control.boundingRect())
    assert not button.boundingRect().intersects(item.submap_control.boundingRect())
    assert not button.boundingRect().intersects(item.fold_control.button_rect())
    point = canvas.view.mapFromScene(button.mapToScene(button.boundingRect().center()))
    qtbot.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert canvas.current_page_id == target_page and canvas.selected_node_ids() == [target]
    back = canvas.path_navigation.back_button
    assert back.isEnabled() and back.toolTip() == "返回引用节点"
    qtbot.mouseClick(back, Qt.MouseButton.LeftButton)
    restored = canvas.capture_page_view_state()
    assert restored["page_id"] == source_page and restored["selection"] == [source]
    assert restored["zoom"] == pytest.approx(state["zoom"])
    assert (QPointF(*restored["center"]) - QPointF(*state["center"])).manhattanLength() < 4
    assert engine.revision == revision and not engine.is_dirty


@pytest.mark.parametrize("inspiration", [False, True])
def test_root_edit_updates_linked_nodes_and_outline_in_other_tab_only_where_needed(
    make_canvas, qtbot, monkeypatch, tmp_path, inspiration,
):
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "旧根正文")
    target_page = engine.create_submap(owner)
    target = engine.page_document(target_page).root_id
    root = engine.add_inspiration("想法", position=(0, 0)) if inspiration else engine.root_id
    first = engine.add_child(root, "甲原文")
    second = engine.add_child(root, "乙原文")
    ordinary = engine.add_child(root, "无关节点")
    for key in (first, second):
        engine.set_page_reference(key, target)
    engine.update_node(first, icons=["★"])
    source_canvas = make_canvas(engine)
    target_canvas = make_canvas(engine)
    if inspiration:
        source_canvas.show_inspiration(root)
    target_canvas.navigate_to(target_page, target)
    outline = OutlinePanel(source_canvas)
    qtbot.addWidget(outline)
    measured = []
    visual = source_canvas.factory.visual

    def measure(node, *args, **kwargs):
        measured.append(node.id)
        return visual(node, *args, **kwargs)

    monkeypatch.setattr(source_canvas.factory, "visual", measure)
    with engine.event_source(target_canvas.interaction_id):
        engine.update_node(target, text="## 跨页正文")
    assert {first, second} <= set(measured) and ordinary not in measured
    assert "##" in source_canvas.node_items[first].visual.text.toPlainText()
    measured.clear()
    with engine.event_source(target_canvas.interaction_id):
        engine.update_node(target, text_format="markdown")
    assert set(measured) == {first, second}
    for key in (first, second):
        item = source_canvas.node_items[key]
        assert "跨页正文" in item.visual.text.toPlainText() and "##" not in item.visual.text.toPlainText()
        assert outline.tree_model.data(outline.tree_model.index_for(key)) == "## 跨页正文"
    assert source_canvas.current_page_id == (engine.inspiration_id if inspiration else None)
    path = tmp_path / "引用正文.svg"
    source_canvas.export(path)
    content = path.read_text(encoding="utf-8")
    assert "跨页正文" in content and "甲原文" not in content and "乙原文" not in content
    with engine.event_source(target_canvas.interaction_id):
        engine.remove_submap(owner)
    assert outline.tree_model.data(outline.tree_model.index_for(first)) == "甲原文"
    assert not source_canvas.node_items[first].reference_control.isVisible()
    with engine.event_source(target_canvas.interaction_id):
        assert engine.undo()
    assert source_canvas.node_items[first].reference_control.isVisible()


def test_clipboard_uses_display_text_for_plain_copy_and_keeps_native_reference(make_canvas):
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "目标正文")
    page = engine.create_submap(owner)
    target = engine.page_document(page).root_id
    source = engine.add_child(engine.root_id, "来源原文")
    engine.set_page_reference(source, target)
    canvas = make_canvas(engine)
    canvas.select_nodes([source])
    canvas.copy()
    plain = QApplication.clipboard().text()
    assert "目标正文" in plain and "来源原文" not in plain
    canvas.select_nodes([engine.root_id])
    copied, = canvas.paste()
    assert engine.resolve_page_reference(copied).node_id == target
    assert engine.get_node(copied).text == "来源原文"


def test_outline_paste_can_replace_branches_but_not_a_reference_body(make_canvas, qtbot):
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "目标正文")
    target = engine.page_document(engine.create_detail_page(owner)).root_id
    source = engine.add_child(engine.root_id, "来源原文")
    child = engine.add_child(source, "旧后代")
    engine.set_page_reference(source, target)
    canvas = make_canvas(engine)
    canvas.select_nodes([source])
    canvas.set_edraw_paste_options("replace", False, True)
    QApplication.clipboard().setText("# 不覆盖正文\n- 新后代")

    def confirm():
        dialog = QApplication.activeModalWidget()
        try:
            assert not dialog.findChild(QPushButton, "edraw_replace_root").isEnabled()
            assert dialog.findChild(QPushButton, "edraw_keep_root").isChecked()
            qtbot.mouseClick(dialog.findChild(QPushButton, "edraw_paste_confirm"), Qt.MouseButton.LeftButton)
        finally:
            if dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, confirm)
    assert canvas.paste() == [source]
    node = engine.get_node(source)
    assert node.text == "来源原文" and node.page_reference_root_id == target
    assert child not in engine.document.nodes and [engine.get_node(key).text for key in node.children] == ["新后代"]
    assert canvas.edraw_paste_options == ("replace", False, True)


def test_other_tab_setting_a_reference_discards_the_now_readonly_inline_draft(make_canvas):
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "目标正文")
    target = engine.page_document(engine.create_detail_page(owner)).root_id
    source = engine.add_child(engine.root_id, "来源原文")
    canvas = make_canvas(engine)
    other = make_canvas(engine)
    canvas.begin_edit(source)
    canvas.editor.setPlainText("未提交的草稿")
    with engine.event_source(other.interaction_id):
        engine.set_page_reference(source, target)
    assert canvas.editor is None
    assert canvas.document_view.nodes[source].text == "目标正文"
    assert engine.get_node(source).text == "来源原文"


def test_deleted_return_source_does_not_revive_when_undone(make_canvas):
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "子图")
    page = engine.create_submap(owner)
    source = engine.add_child(engine.page_document(page).root_id, "链接回主图")
    engine.set_page_reference(source, engine.root_id)
    canvas = make_canvas(engine)
    canvas.navigate_to(page, source)
    canvas.open_page_reference(source)
    back = canvas.path_navigation.back_button
    assert back.isEnabled()
    engine.remove_nodes([source])
    assert canvas.current_page_id is None and not back.isEnabled()
    assert engine.undo() and not back.isEnabled()
