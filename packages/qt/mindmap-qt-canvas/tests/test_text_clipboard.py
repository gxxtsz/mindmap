"""文本大纲的自动识别、共用配置弹窗、多根插入及一次撤销重做验收。"""

from copy import deepcopy

import pytest
from mindmap_core import FormatRegistry, MindMapEngine, MindMapError
from mindmap_formats import OutlineCodec, create_default_format_registry
from mindmap_qt_canvas import MindMapWidget
from mindmap_qt_ui.dialogs import EdrawPasteOptions, edraw_paste_options
from PySide6.QtCore import QMimeData, QPoint, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QWidget,
)


@pytest.fixture
def canvas(qtbot, qapp, monkeypatch):
    """隔离画布与系统剪贴板，保留一个可追加节点和一个无关分支。"""
    original = QMimeData()
    mime = qapp.clipboard().mimeData()
    if mime is not None:
        for kind in mime.formats():
            original.setData(kind, mime.data(kind))
    engine = MindMapEngine()
    target = engine.add_child(engine.root_id, "粘贴目标")
    engine.add_child(engine.root_id, "无关分支")
    engine.replace_document(engine.document)
    widget = MindMapWidget(engine)
    qtbot.addWidget(widget)
    widget.resize(1000, 700)
    widget.show()
    widget.select_nodes([target])
    QApplication.processEvents()
    # 解析及扩展测试默认选择追加；真实界面用例显式恢复弹窗并用 Qt 事件完成交互。
    monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options",
                        lambda *args, **kwargs: EdrawPasteOptions("append", True, True))
    yield widget, target
    widget.finish_edit(commit=False)
    widget.dispose()
    if qapp.platformName() == "offscreen":
        qapp.clipboard().clear()
    else:
        qapp.clipboard().setMimeData(original)


def paste_from_entry(widget, entry, qtbot, monkeypatch):
    """执行画布 Ctrl+V 或真实右键菜单构建后的粘贴动作。"""
    if entry == "shortcut":
        widget.activateWindow()
        widget.view.setFocus()
        qtbot.keyClick(widget.view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    else:
        class PasteMenu(QMenu):
            def exec(self, position):
                assert widget.action("paste") in self.actions()
                widget.action("paste").trigger()
        monkeypatch.setattr("mindmap_qt_canvas.widget.QMenu", PasteMenu)
        widget.context_menu(QPoint(20, 20))


@pytest.mark.parametrize("page_kind", ["main", "submap", "detail"])
@pytest.mark.parametrize("entry", ["shortcut", "context"])
@pytest.mark.parametrize("indent", ["\t", "  ", "    "])
def test_indented_forest_pastes_atomically_through_existing_entries(
    canvas, qtbot, monkeypatch, page_kind, entry, indent,
):
    """三个页面的两种入口均保持多根层级、原有子节点、选择及单次历史。"""
    widget, _ = canvas
    if page_kind == "submap":
        widget.create_submap()
    elif page_kind == "detail":
        widget.create_detail_page()
    target = widget.current_node_id()
    old_child = widget.engine.add_child(target, "已有节点")
    widget.engine.set_expanded(target, False)
    widget.select_nodes([target])
    QApplication.clipboard().setText(
        f"开发计划\n{indent}前端\n{indent * 2}页面\n{indent}后端\n测试计划\n{indent}功能测试"
    )
    before, revision = widget.engine.document, widget.engine.revision
    selection, page = widget.selected_node_ids(), widget.current_page_id
    errors = []
    widget.error_occurred.connect(errors.append)
    widget.set_edraw_paste_options("replace", False, False)
    monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options", edraw_paste_options)

    def accept_multiple_roots():
        dialog = QApplication.activeModalWidget()
        try:
            assert isinstance(dialog, QDialog) and dialog.windowTitle() == "粘贴缩进大纲"
            assert not dialog.findChild(QAbstractButton, "edraw_replace").isEnabled()
            assert not dialog.findChild(QWidget, "edraw_root_options").isVisible()
            assert not dialog.findChild(QWidget, "edraw_descendants").isVisible()
            assert any("2 个顶层主题，共 6 个节点" in label.text() for label in dialog.findChildren(QLabel))
            qtbot.mouseClick(dialog.findChild(QPushButton, "edraw_paste_confirm"), Qt.MouseButton.LeftButton)
        finally:
            if dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, accept_multiple_roots)
    paste_from_entry(widget, entry, qtbot, monkeypatch)

    document = widget.engine.document
    assert not errors
    roots = document.nodes[target].children[1:]
    assert len(roots) == 2 and document.nodes[target].children[0] == old_child
    assert [document.nodes[key].text for key in roots] == ["开发计划", "测试计划"]
    children = document.nodes[roots[0]].children
    assert [document.nodes[key].text for key in children] == ["前端", "后端"]
    assert document.nodes[document.nodes[children[0]].children[0]].text == "页面"
    assert document.nodes[document.nodes[roots[1]].children[0]].text == "功能测试"
    assert len(document.nodes) == len(before.nodes) + 6
    assert document.nodes[target].expanded and widget.current_page_id == page
    assert widget.selected_node_ids() == roots and widget.engine.revision == revision + 1
    assert widget.edraw_paste_options == ("replace", False, False)
    assert widget.engine.undo() and widget.engine.document == before
    assert widget.selected_node_ids() == selection and widget.current_page_id == page
    assert widget.engine.redo() and widget.engine.document == document
    assert widget.selected_node_ids() == roots and widget.current_page_id == page


@pytest.mark.parametrize("entry", ["shortcut", "context"])
@pytest.mark.parametrize("text,line", [
    ("甲\n  乙\n      丙", 3),
    ("\n甲\n\n  乙\n     丙", 5),
    ("甲\n\t乙\n    丙", 3),
    ("# 根\n\n      - 跳级", 3),
    ("# 根\n普通文字", 2),
])
def test_invalid_outline_reports_line_without_mutating_document_or_history(
    canvas, qtbot, monkeypatch, entry, text, line,
):
    """已识别格式的错误不降级为纯文本，两个入口都报告原行号且完整保持现场。"""
    widget, _ = canvas
    QApplication.clipboard().setText(text)
    before, revision = widget.engine.document, widget.engine.revision
    selection = widget.selected_node_ids()
    errors = []
    widget.error_occurred.connect(errors.append)
    monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options",
                        lambda *args, **kwargs: pytest.fail("解析失败不能弹出配置"))
    paste_from_entry(widget, entry, qtbot, monkeypatch)
    assert len(errors) == 1 and f"第 {line} 行：" in errors[0]
    assert widget.engine.document == before and widget.engine.revision == revision
    assert widget.selected_node_ids() == selection and not widget.engine.can_undo
    assert not widget.engine.can_redo and QApplication.clipboard().text() == text


@pytest.mark.parametrize("text,root,child", [
    ("# Markdown\n- 子节点", "Markdown", "子节点"),
    ("+ 一级\n  + 二级", "一级", "二级"),
    ("1. 一级\n  2. 二级", "一级", "二级"),
    ("1) 一级\n  2) 二级", "一级", "二级"),
    ("\ufeff\n# Markdown\n- 子节点", "Markdown", "子节点"),
    ("#标签\n  -正文", "#标签", "-正文"),
])
def test_text_paste_detects_markdown_and_preserves_plain_symbol_prefixes(canvas, text, root, child):
    widget, _ = canvas
    QApplication.clipboard().setText(text)
    inserted, = widget.paste()
    node = widget.engine.get_node(inserted)
    assert node.text == root and len(node.children) == 1
    assert widget.engine.get_node(node.children[0]).text == child


def test_indented_paste_keeps_redo_on_parse_or_engine_failure(canvas):
    """解析失败或引擎拒绝都不覆盖已有撤销／重做记录。"""
    widget, target = canvas
    engine = widget.engine
    engine.add_child(target, "可重做节点")
    engine.undo()
    before, revision = engine.document, engine.revision
    selection = widget.selected_node_ids()
    QApplication.clipboard().setText("甲\n   乙")
    with pytest.raises(MindMapError, match="第 2 行"):
        widget.paste()
    assert engine.document == before and engine.revision == revision
    assert widget.selected_node_ids() == selection and engine.can_redo
    QApplication.clipboard().setText("甲\n乙")
    engine.before_operation = lambda operation, args: operation != "paste_fragment"
    with pytest.raises(MindMapError):
        widget.paste()
    assert engine.document == before and engine.revision == revision
    assert widget.selected_node_ids() == selection and engine.can_redo


def test_indented_paste_in_readonly_and_text_editor_preserves_existing_behavior(canvas, qtbot, monkeypatch):
    widget, target = canvas
    text = "甲\n  孩子\n乙"
    QApplication.clipboard().setText(text)
    before = widget.engine.document
    monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options",
                        lambda *args, **kwargs: pytest.fail("只读或正文编辑不能弹出配置"))
    widget.set_editable(False)
    assert not widget.action("paste").isEnabled()
    with pytest.raises(MindMapError, match="只读"):
        widget.paste()
    assert widget.engine.document == before and not widget.engine.can_undo
    widget.set_editable(True)
    widget.begin_edit(target)
    qtbot.keyClick(widget.view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert widget.editor.toPlainText() == text
    assert widget.engine.document == before and not widget.engine.can_undo


def test_indented_paste_without_selection_creates_independent_inspiration_trees(canvas, monkeypatch):
    """灵感区空态的多根片段不留下临时容器，整组树及落点一次撤销重做。"""
    widget, _ = canvas
    widget.show_inspiration()
    before, revision = widget.engine.document, widget.engine.revision
    assert not widget.selected_node_ids()
    monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options",
                        lambda *args, **kwargs: pytest.fail("无目标的游离树粘贴不能弹出配置"))
    QApplication.clipboard().setText("甲\n    孩子甲\n乙\n    孩子乙")
    roots = widget.paste()
    document = widget.engine.document
    assert len(roots) == 2 and document.inspiration.root_ids == roots
    assert [document.nodes[key].text for key in roots] == ["甲", "乙"]
    assert [document.nodes[document.nodes[key].children[0]].text for key in roots] == ["孩子甲", "孩子乙"]
    assert len(document.nodes) == len(before.nodes) + 4
    assert set(document.inspiration.positions) == set(roots)
    assert widget.engine.revision == revision + 1
    assert widget.engine.undo() and widget.engine.document == before
    assert not widget.selected_node_ids()
    assert widget.engine.redo() and widget.engine.document == document


def test_indented_paste_passes_fragment_option_to_injected_text_codec(canvas):
    """文本仍通过宿主注入的编解码器；不能在画布内绕过扩展实现自行解析。"""
    class TrackingText(OutlineCodec):
        def __init__(self):
            super().__init__(markdown=False)
            self.options = []

        def read(self, stream, *, options):
            self.options.append(options)
            return super().read(stream, options=options)

    widget, _ = canvas
    codec = TrackingText()
    registry = FormatRegistry()
    for entry in create_default_format_registry().list():
        registry.register(
            entry.info, importer=codec if entry.info.id == "text" else entry.importer, exporter=entry.exporter,
        )
    widget.format_registry = registry
    QApplication.clipboard().setText("甲\n乙")
    roots = widget.paste()
    assert codec.options == [{"as_fragment": True}]
    assert [widget.engine.get_node(key).text for key in roots] == ["甲", "乙"]


SINGLE_TREE_SOURCES = [
    ("来源\n  子一\n    孙节点\n  子二", "粘贴缩进大纲"),
    ("# 来源\n- 子一\n  - 孙节点\n- 子二", "粘贴 Markdown 大纲"),
]


@pytest.mark.parametrize("text,title", SINGLE_TREE_SOURCES)
@pytest.mark.parametrize("entry", ["shortcut", "context"])
@pytest.mark.parametrize("mode", ["append", "replace"])
@pytest.mark.parametrize("keep_children", [False, True])
@pytest.mark.parametrize("replace_root", [False, True])
def test_text_options_dialog_applies_all_choices_with_one_undo(
    canvas, qtbot, monkeypatch, text, title, entry, mode, keep_children, replace_root,
):
    """两种文本的真实弹窗支持全部选择组合，保留目标属性并独立继承新分支样式。"""
    widget, target = canvas
    engine = widget.engine
    old_child = engine.add_child(target, "旧后代")
    old_grandchild = engine.add_child(old_child, "旧孙节点")
    engine.update_node(target, style={"font_size": 19, "branch": {"kind": "line"}},
                       text_format="markdown", tags=["目标标签"], note="目标备注")
    engine.replace_document(engine.document)
    widget.select_nodes([target])
    before, revision = engine.document, engine.revision
    monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options", edraw_paste_options)
    QApplication.clipboard().setText(text)

    def choose():
        dialog = QApplication.activeModalWidget()
        try:
            assert isinstance(dialog, QDialog) and dialog.windowTitle() == title
            assert any("1 个顶层主题，共 4 个节点" in label.text() for label in dialog.findChildren(QLabel))
            qtbot.mouseClick(dialog.findChild(QAbstractButton, "edraw_replace"), Qt.MouseButton.LeftButton)
            root_button = "edraw_replace_root" if replace_root else "edraw_keep_root"
            children_button = "edraw_keep_children" if keep_children else "edraw_remove_children"
            qtbot.mouseClick(dialog.findChild(QPushButton, root_button), Qt.MouseButton.LeftButton)
            qtbot.mouseClick(dialog.findChild(QPushButton, children_button), Qt.MouseButton.LeftButton)
            if mode == "append":
                qtbot.mouseClick(dialog.findChild(QAbstractButton, "edraw_append"), Qt.MouseButton.LeftButton)
                assert not dialog.findChild(QWidget, "edraw_root_options").isVisible()
                assert not dialog.findChild(QWidget, "edraw_descendants").isVisible()
            confirm = dialog.findChild(QPushButton, "edraw_paste_confirm")
            assert confirm.text() == ("替换粘贴" if mode == "replace" else "追加粘贴")
            qtbot.mouseClick(confirm, Qt.MouseButton.LeftButton)
        finally:
            if dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, choose)
    paste_from_entry(widget, entry, qtbot, monkeypatch)
    document = engine.document
    replacing = mode == "replace"
    node = document.nodes[target]
    expected = deepcopy(before.nodes[target])
    if replacing and replace_root:
        expected.text, expected.text_format = "来源", "plain"
    old_children = before.nodes[target].children if not replacing or keep_children else []
    assert node.children[:len(old_children)] == old_children
    inserted = node.children[len(old_children):]
    assert [document.nodes[key].text for key in inserted] == (["子一", "子二"] if replacing else ["来源"])
    source = node if replacing else document.nodes[inserted[0]]
    source_children = inserted if replacing else source.children
    assert [document.nodes[key].text for key in source_children] == ["子一", "子二"]
    assert document.nodes[document.nodes[source_children[0]].children[0]].text == "孙节点"
    expected.children = node.children
    assert node == expected
    assert (old_child in document.nodes and old_grandchild in document.nodes) == (not replacing or keep_children)
    added = document.nodes.keys() - before.nodes.keys()
    assert len(added) == (3 if replacing else 4)
    assert all(document.nodes[key].style["branch"] == {"kind": "line"} for key in added)
    assert len({id(document.nodes[key].style["branch"]) for key in added}) == len(added)
    selection = [target] if replacing else inserted
    assert widget.selected_node_ids() == selection and engine.revision == revision + 1
    assert widget.edraw_paste_options == (mode, keep_children, replace_root)
    assert engine.undo() and engine.document == before and not engine.can_undo
    assert widget.selected_node_ids() == [target]
    assert widget.edraw_paste_options == (mode, keep_children, replace_root)
    assert engine.redo() and engine.document == document and widget.selected_node_ids() == selection


@pytest.mark.parametrize("text,title", SINGLE_TREE_SOURCES)
@pytest.mark.parametrize("page_kind", ["submap", "detail", "inspiration"])
def test_text_replacement_on_other_pages_keeps_identity_and_history(canvas, monkeypatch, text, title, page_kind):
    widget, target = canvas
    engine = widget.engine
    if page_kind == "submap":
        widget.create_submap()
    elif page_kind == "detail":
        widget.create_detail_page()
    else:
        target = engine.add_inspiration("独立想法", position=(40, 60))
        widget.show_inspiration(target)
    target = widget.current_node_id()
    old_child = engine.add_child(target, "旧后代")
    widget.select_nodes([target])
    before, revision, page = engine.document, engine.revision, widget.current_page_id
    monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options",
                        lambda *args, **kwargs: EdrawPasteOptions("replace", False, True))
    QApplication.clipboard().setText(text)
    assert widget.paste() == [target]
    after = engine.document
    assert engine.get_node(target).text == "来源" and old_child not in after.nodes
    assert widget.selected_node_ids() == [target] and widget.current_page_id == page
    assert engine.revision == revision + 1 and widget.edraw_paste_options == ("replace", False, True)
    assert engine.undo() and engine.document == before
    assert widget.selected_node_ids() == [target] and widget.current_page_id == page
    assert engine.redo() and engine.document == after
    assert widget.selected_node_ids() == [target] and widget.current_page_id == page


@pytest.mark.parametrize("text", ["单行主题", "# 单行主题"])
@pytest.mark.parametrize("keep_children", [False, True])
@pytest.mark.parametrize("replace_root", [False, True])
def test_single_line_replacement_respects_noop_and_shared_preferences(
    canvas, monkeypatch, text, keep_children, replace_root,
):
    """无来源后代时仍询问；保留目标且不移除已有内容的空操作仅记忆选项。"""
    widget, target = canvas
    engine = widget.engine
    old_child = engine.add_child(target, "旧后代")
    engine.replace_document(engine.document)
    widget.select_nodes([target])
    before, revision = engine.document, engine.revision
    prompts = []

    def choose(*args, **kwargs):
        prompts.append(kwargs)
        return EdrawPasteOptions("replace", keep_children, replace_root)

    monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options", choose)
    QApplication.clipboard().setText(text)
    assert widget.paste() == [target]
    after = engine.document
    assert len(prompts) == 1 and prompts[0]["root_count"] == prompts[0]["node_count"] == 1
    assert (old_child in after.nodes) == keep_children
    assert engine.get_node(target).text == ("单行主题" if replace_root else "粘贴目标")
    changed = replace_root or not keep_children
    assert engine.revision == revision + int(changed) and engine.can_undo == changed
    assert widget.edraw_paste_options == ("replace", keep_children, replace_root)
    if changed:
        assert engine.undo() and engine.document == before
        assert engine.redo() and engine.document == after
    else:
        assert after == before and not engine.is_dirty and not widget._selection_undo


def paste_snapshot(widget):
    """记录内容、历史、选择及共享配置，确保失败不留下本次粘贴的任何状态。"""
    engine = widget.engine
    return (engine.document, engine.revision, engine.is_dirty, engine.can_undo, engine.can_redo,
            widget.current_page_id, widget.selected_node_ids(), deepcopy(widget._selection_undo),
            deepcopy(widget._selection_redo), widget.edraw_paste_options)


@pytest.mark.parametrize("text,title", SINGLE_TREE_SOURCES)
@pytest.mark.parametrize("failure", ["cancel", "engine", "mode", "keep", "root"])
def test_text_dialog_cancel_and_failed_replacement_preserve_complete_state(canvas, monkeypatch, text, title, failure):
    widget, target = canvas
    engine = widget.engine
    engine.add_child(target, "旧后代")
    engine.add_child(target, "可重做节点")
    engine.undo()
    widget.set_edraw_paste_options("append", False, False)
    before = paste_snapshot(widget)
    option = {
        "cancel": None,
        "engine": EdrawPasteOptions("replace", False, True),
        "mode": EdrawPasteOptions("invalid", False, True),
        "keep": EdrawPasteOptions("replace", 0, True),
        "root": EdrawPasteOptions("replace", False, "false"),
    }[failure]
    monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options", lambda *args, **kwargs: option)
    if failure == "engine":
        engine.before_operation = lambda operation, args: operation != "paste_fragment"
    QApplication.clipboard().setText(text)
    if failure == "cancel":
        assert widget.paste() == []
    else:
        with pytest.raises(MindMapError):
            widget.paste()
    assert paste_snapshot(widget) == before and QApplication.clipboard().text() == text


@pytest.mark.parametrize("text,title", SINGLE_TREE_SOURCES)
@pytest.mark.parametrize("change", ["document", "revision", "page", "target", "readonly"])
def test_text_dialog_rejects_changed_document_page_or_target(canvas, monkeypatch, text, title, change):
    """模态弹窗中的外部操作可以保留，但不能再将旧选项套到已经改变的现场。"""
    widget, target = canvas
    engine = widget.engine
    widget.create_detail_page()
    widget.return_to_parent_page()
    widget.select_nodes([target])
    after_change = []

    def choose(*args, **kwargs):
        if change == "document":
            engine.replace_document(MindMapEngine().document)
        elif change == "revision":
            engine.add_child(target, "弹窗期间新建")
        elif change == "page":
            widget.enter_detail_page(target)
        elif change == "target":
            widget.select_nodes([engine.root_id])
        else:
            widget.set_editable(False)
        after_change.append(paste_snapshot(widget))
        return EdrawPasteOptions("replace", False, True)

    monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options", choose)
    QApplication.clipboard().setText(text)
    with pytest.raises(MindMapError, match="只读|已发生变化"):
        widget.paste()
    assert paste_snapshot(widget) == after_change[0]


def test_multiple_text_roots_reject_non_ui_replace_choice(canvas, monkeypatch):
    widget, _ = canvas
    QApplication.clipboard().setText("甲\n乙")
    before = paste_snapshot(widget)
    monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options",
                        lambda *args, **kwargs: EdrawPasteOptions("replace", False, True))
    with pytest.raises(MindMapError, match="多个并列主题只能追加"):
        widget.paste()
    assert paste_snapshot(widget) == before


@pytest.mark.parametrize("mode", ["append", "replace"])
@pytest.mark.parametrize("accept", [False, True])
def test_injected_text_fragment_keeps_detail_page_confirmation_and_source_data(canvas, monkeypatch, mode, accept):
    """扩展文本片段仍遵守详情页省略确认；配置流程传递页面选项且不改写来源样式或页面。"""
    widget, _ = canvas
    widget.create_detail_page()
    target = widget.current_node_id()
    widget.engine.update_node(target, style={"branch": {"kind": "line"}})
    source = MindMapEngine()
    branch = source.add_child(source.root_id, "来源")
    owner = source.add_child(branch, "附属页入口")
    source.create_submap(owner)
    source.create_detail_page(owner)
    fragment = source.copy_fragment([branch])
    original = deepcopy(fragment)
    reads, prompts = [], []

    class PageFragmentCodec:
        def read(self, stream, *, options):
            reads.append(options)
            return fragment

    registry = FormatRegistry()
    registry.register(create_default_format_registry().get("text").info, importer=PageFragmentCodec())
    widget.format_registry = registry

    def confirm(*args):
        prompts.append(args[2])
        return QMessageBox.StandardButton.Yes if accept else QMessageBox.StandardButton.No

    def choose(*args, **kwargs):
        assert accept, "取消附属页面省略后不能继续弹出粘贴配置"
        assert kwargs["title"] == "粘贴缩进大纲"
        return EdrawPasteOptions(mode, False, True)

    monkeypatch.setattr(QMessageBox, "question", confirm)
    monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options", choose)
    QApplication.clipboard().setText("扩展片段")
    before = paste_snapshot(widget)
    inserted = widget.paste()
    assert reads == [{"as_fragment": True}] and prompts and "子导图" in prompts[0] and "详情页" in prompts[0]
    assert fragment == original
    if accept:
        document = widget.engine.document
        assert inserted and widget.current_page_kind == "detail"
        assert document.submaps == before[0].submaps
        assert document.detail_pages == before[0].detail_pages
        assert all(not node.submap_id and not node.detail_page_id for node in widget.document_view.nodes.values())
        assert widget.engine.undo() and widget.engine.document == before[0]
    else:
        assert not inserted and paste_snapshot(widget) == before
