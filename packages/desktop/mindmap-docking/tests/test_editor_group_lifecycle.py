"""空分屏生命周期：关闭、迁移和旧布局恢复后只保留必要的编辑区域。"""

from copy import deepcopy

import pytest
from mindmap_docking import DockTarget
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QLabel, QPlainTextEdit
from shiboken6 import isValid


def add_split_document(docking, token, orientation):
    """用真实文档创建第二个分屏，保留内容实例以验证收起分屏不会销毁业务内容。"""
    content = QPlainTextEdit("保留内容：" + token)
    docking.manager.add_document(token, token, content)
    docking.host.editor.split_token(token, orientation)
    return docking.host.editor.find_token(token)[0], content


@pytest.mark.parametrize("orientation", [Qt.Orientation.Horizontal, Qt.Orientation.Vertical])
def test_close_nested_split_reclaims_space_and_keeps_cached_documents(docking, orientation):
    """逐层关闭嵌套分屏时清除多余分割层级，剩余文档铺满编辑区且关闭文档可重开。"""
    manager, area = docking.manager, docking.host.editor
    original = area.groups[0]
    second, content = add_split_document(docking, "second", orientation)
    third, _ = add_split_document(docking, "third", Qt.Orientation.Vertical)
    assert len(area.groups) == 3

    assert manager.close_document("third")
    assert len(area.groups) == 2 and third not in area.groups
    assert area.find_token("doc")[0] is original
    assert area.find_token("second")[0] is second
    assert manager.close_document("second")
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()
    assert not isValid(second) and not isValid(third) and isValid(content)
    assert area.groups == [original]
    assert area.root.count() == 1 and area.root.widget(0) is original
    assert original.width() == area.width() and original.height() == area.height()
    assert manager.active_document_id == "doc"
    assert original.currentWidget() is docking.document
    manager.open_document("second")
    assert original.currentWidget() is content
    assert content.toPlainText() == "保留内容：second"


def test_closing_neighbor_keeps_current_document_focus(docking, qtbot):
    """收起相邻分屏不夺走正在编辑的文档焦点，后续输入继续进入原控件。"""
    manager = docking.manager
    add_split_document(docking, "second", Qt.Orientation.Horizontal)
    manager.activate_document("doc")
    qtbot.waitUntil(docking.document.hasFocus)
    assert manager.close_document("second")
    QApplication.processEvents()
    assert docking.document.hasFocus() and manager.active_document_id == "doc"


def test_removing_nested_split_keeps_neighbor_size(docking):
    """合并内部单子分支时，外层相邻文档的宽度不因 Qt 重新分配尺寸而跳动。"""
    manager, area = docking.manager, docking.host.editor
    second, _ = add_split_document(docking, "second", Qt.Orientation.Horizontal)
    add_split_document(docking, "third", Qt.Orientation.Vertical)
    outer = second.parentWidget()
    outer.setSizes([300, 700])
    QApplication.processEvents()
    sizes = outer.sizes()
    assert manager.close_document("third")
    QApplication.processEvents()
    assert outer.sizes() == sizes
    assert area.find_token("second")[0] is second


def test_moving_last_tab_merges_source_group_and_preserves_target_selection(docking):
    """跨组迁移完成后才收起源分屏，目标顺序、当前标签和内容实例保持有效。"""
    manager, area = docking.manager, docking.host.editor
    target = area.groups[0]
    source, content = add_split_document(docking, "second", Qt.Orientation.Horizontal)
    manager.move_document("second", "main", target.group_id, 0)
    assert area.groups == [target] and source not in area.groups
    assert area.root.count() == 1 and area.root.widget(0) is target
    assert [target.tabBar().tabData(index) for index in range(target.count())] == ["second", "doc"]
    assert target.currentWidget() is content and manager.active_document_id == "second"


def test_rejected_close_preserves_split_and_document(docking):
    """保存检查拒绝关闭时不提前收起分屏，也不改变文档内容或布局比例。"""
    manager, area = docking.manager, docking.host.editor
    group, content = add_split_document(docking, "second", Qt.Orientation.Horizontal)
    manager.set_document_modified("second", True)
    before = manager.capture_layout()
    assert not manager.close_document("second")
    assert manager.capture_layout() == before
    assert len(area.groups) == 2 and area.find_token("second")[0] is group
    assert group.currentWidget() is content and content.toPlainText() == "保留内容：second"


def test_batch_close_leaves_one_welcome_group(docking):
    """批量关闭多个分屏的全部标签后仅保留一份欢迎页，随后打开仍复用该组。"""
    manager, area = docking.manager, docking.host.editor
    area.set_empty_widget_factory(lambda: QLabel("新建或打开导图"))
    add_split_document(docking, "second", Qt.Orientation.Horizontal)
    add_split_document(docking, "third", Qt.Orientation.Vertical)
    assert manager.close_documents(["doc", "second", "third"])
    QApplication.processEvents()
    assert len(area.groups) == 1
    group = area.groups[0]
    assert group.count() == 0 and group.empty_widget.isVisible()
    assert area.root.count() == 1 and area.root.widget(0) is group
    assert manager.active_document_id == ""
    manager.open_document("doc")
    assert area.groups == [group] and group.currentWidget() is docking.document
    assert not group.empty_widget.isVisible()


def test_tool_only_group_survives_refresh_and_closes_when_hidden(docking):
    """工具刷新中的临时空组必须保留；真正隐藏最后一个工具标签后才回收分屏。"""
    manager, area = docking.manager, docking.host.editor
    original = area.groups[0]
    manager.move("alpha", DockTarget("editor"))
    content = manager.panel("alpha").content
    area.split_token("tool:alpha", Qt.Orientation.Vertical)
    group = area.find_token("tool:alpha")[0]
    for _ in range(2):
        manager.refresh()
        assert len(area.groups) == 2 and area.find_token("tool:alpha")[0] is group
    manager.hide("alpha")
    assert area.groups == [original]
    assert manager.panel("alpha").content is content
    assert manager.states["alpha"].editor_group == original.group_id
    manager.activate("alpha", focus=False)
    assert area.find_token("tool:alpha")[0] is original
    assert manager.panel("alpha").content is content


@pytest.mark.parametrize("keep_document", [False, True])
def test_restore_old_layout_removes_empty_and_unknown_document_groups(docking, keep_document):
    """历史快照中的空分屏和失效文档不再占位，恢复过程不修改调用方的快照。"""
    manager, area = docking.manager, docking.host.editor
    area.set_empty_widget_factory(lambda: QLabel("新建或打开导图"))
    snapshot = manager.capture_layout()
    snapshot["hosts"]["main"]["editor"] = {
        "kind": "split", "orientation": "horizontal", "sizes": [300, 400, 300],
        "children": [
            {"kind": "tabs", "id": "first", "tokens": ["doc"] if keep_document else [], "current": 0},
            {"kind": "tabs", "id": "empty", "tokens": [], "current": -1},
            {"kind": "split", "orientation": "vertical", "sizes": [200, 200], "children": [
                {"kind": "tabs", "id": "missing", "tokens": ["removed_document"], "current": 0},
                {"kind": "tabs", "id": "nested-empty", "tokens": [], "current": -1},
            ]},
        ],
    }
    before = deepcopy(snapshot)
    manager.restore_layout(snapshot)
    QApplication.processEvents()
    assert snapshot == before
    assert len(area.groups) == 1
    group = area.groups[0]
    assert area.root.count() == 1 and area.root.widget(0) is group
    assert group.count() == int(keep_document)
    assert group.empty_widget.isVisible() is not keep_document
    assert (group.currentWidget() is docking.document) is keep_document
    manager.restore_layout(manager.capture_layout())
    assert len(area.groups) == 1 and area.groups[0].count() == int(keep_document)


@pytest.mark.parametrize("orientation", [Qt.Orientation.Horizontal, Qt.Orientation.Vertical])
def test_splitting_only_tab_does_not_create_empty_group(docking, orientation):
    """唯一标签没有可分离的同组页面，拆分操作直接保持原布局和业务实例。"""
    manager, area = docking.manager, docking.host.editor
    before = manager.capture_layout()
    group = area.groups[0]
    area.split_token("doc", orientation)
    assert manager.capture_layout() == before
    assert area.groups == [group] and group.currentWidget() is docking.document
