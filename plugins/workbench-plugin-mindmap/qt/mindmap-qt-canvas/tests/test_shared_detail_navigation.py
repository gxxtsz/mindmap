"""共享详情的实际按钮、视图返回、跨标签刷新与导出验收。"""

import pytest
from mindmap_core import LayoutConfig, LayoutInfo, LayoutResult, MindMapEngine, MindMapError, Rect, Theme
from mindmap_formats import load_document, save_document
from mindmap_qt_canvas import MindMapWidget
from mindmap_qt_render import VisualFactory, rendering
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtWidgets import QApplication, QMessageBox


@pytest.fixture
def shared(qtbot):
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "Rust")
    submap = engine.create_submap(owner)
    root = engine.page_document(submap).root_id
    canvas = MindMapWidget(engine, toolbar=False)
    qtbot.addWidget(canvas)
    canvas.resize(1000, 650)
    canvas.show()
    QApplication.processEvents()
    errors = []
    canvas.error_occurred.connect(errors.append)
    yield canvas, owner, submap, root
    canvas.finish_edit(commit=False)
    canvas.dispose()
    assert not errors


@pytest.mark.parametrize("readonly", [False, True])
def test_root_icon_opens_shared_detail_and_returns_to_original_view(shared, qtbot, readonly):
    """完整按钮留白可点击，只读也能进入，返回恢复子图的缩放、位置和选择。"""
    canvas, owner, submap, root = shared
    detail = canvas.engine.create_detail_page(owner)
    canvas.engine.add_child(root, "所有权")
    canvas.navigate_to(submap)
    QApplication.processEvents()
    canvas.view.zoom(1.4)
    canvas.view.update_navigation_rect(center=QPointF(120, 80))
    canvas.select_nodes([root])
    canvas.set_editable(not readonly)
    canvas.engine.mark_saved()
    revision = canvas.engine.revision
    center = canvas.view.mapToScene(canvas.view.viewport().rect()).boundingRect().center()
    item = canvas.node_items[root]
    control = item.detail_page_control
    assert control.isVisible() and not item.submap_control.isVisible()
    assert not canvas.action("create_detail_page").isVisible()
    assert canvas.action("enter_detail_page").isEnabled()
    assert canvas.action("delete_detail_page").isEnabled() is (not readonly)
    text = QRectF(item.visual.text_position, item.visual.text.size())
    assert not text.intersects(control.boundingRect())
    assert not item.fold_control.button_rect().intersects(control.boundingRect())
    assert control.boundingRect().width() == control.boundingRect().height() == 32
    assert item.visual.page_icon_color == item.visual.text_color
    position = canvas.view.mapFromScene(control.mapToScene(control.boundingRect().topLeft() + QPointF(3, 3)))
    # 参数化窗口的按钮坐标相同，先移出以确保离屏平台也发送一次真实移动事件。
    qtbot.mouseMove(canvas.view.viewport(), canvas.view.viewport().rect().bottomRight())
    qtbot.mouseMove(canvas.view.viewport(), position)
    QApplication.processEvents()
    assert canvas.view.viewport().cursor().shape() == Qt.CursorShape.PointingHandCursor
    qtbot.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=position)
    assert canvas.current_page_id == detail and canvas.editor is None
    assert not canvas.view.node_drag.active
    assert canvas.path_navigation.back_button.toolTip() == "返回所属子导图"
    assert [canvas.engine.document.page_kind(entry.page_id)
            for entry in canvas.path_navigation.entries] == ["main", "detail"]
    qtbot.mouseClick(canvas.path_navigation.back_button, Qt.MouseButton.LeftButton)
    restored = canvas.view.mapToScene(canvas.view.viewport().rect()).boundingRect().center()
    assert canvas.current_page_id == submap and canvas.selected_node_ids() == [root]
    assert canvas.view.transform().m11() == 1.4 and (center - restored).manhattanLength() < 3
    assert canvas.engine.revision == revision and not canvas.engine.is_dirty


def test_create_delete_cancel_and_history_use_same_page_from_root(shared, monkeypatch):
    """子图根创建归属入口；取消不改内容，删除与历史更新根图标和测量宽度。"""
    canvas, owner, submap, root = shared
    canvas.navigate_to(submap)
    baseline = canvas.node_items[root].visual.width
    detail = canvas.create_detail_page()
    assert canvas.engine.document.detail_pages[detail].owner_id == owner
    canvas.return_to_parent_page()
    assert canvas.current_page_id == submap
    assert canvas.node_items[root].visual.width == baseline + 40
    before = canvas.engine.document
    with pytest.raises(MindMapError, match="已有详情页"):
        canvas.create_detail_page()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    canvas.delete_detail_page()
    assert canvas.engine.document == before
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    canvas.delete_detail_page()
    assert not canvas.engine.document.detail_pages
    assert canvas.current_page_id == submap and canvas.node_items[root].visual.width == baseline
    assert canvas.engine.undo() and canvas.engine.document == before
    canvas.navigate_to(submap)
    assert canvas.node_items[root].detail_page_control.isVisible()
    assert canvas.engine.redo()
    canvas.navigate_to(submap)
    assert not canvas.node_items[root].detail_page_control.isVisible()
    assert canvas.node_items[root].visual.width == baseline


def test_shared_tabs_refresh_offpage_aliases_and_keep_their_own_return_origins(shared, qtbot):
    """入口变化即时更新另一标签的子图根，两个视图的详情返回来源互不覆盖。"""
    canvas, owner, submap, root = shared
    canvas.shared_views = True
    canvas.navigate_to(submap)
    baseline = canvas.node_items[root].visual.width
    other = MindMapWidget(canvas.engine, toolbar=False)
    other.shared_views = True
    qtbot.addWidget(other)
    other.resize(800, 550)
    other.show()
    QApplication.processEvents()
    try:
        other.select_nodes([owner])
        detail = other.create_detail_page()
        assert canvas.current_page_id == submap
        assert canvas.node_items[root].detail_page_control.isVisible()
        assert canvas.node_items[root].visual.width == baseline + 40
        assert canvas.layout_result.nodes[root].width == baseline + 40
        canvas.enter_detail_page(root)
        assert canvas.current_page_id == other.current_page_id == detail
        other.return_to_parent_page()
        assert other.current_page_id is None
        canvas.return_to_parent_page()
        assert canvas.current_page_id == submap
        with canvas.engine.event_source(other.interaction_id):
            canvas.engine.remove_detail_page(owner)
        assert canvas.current_page_id == submap and canvas.node_items[root].visual.width == baseline
        assert not canvas.node_items[root].detail_page_control.isVisible()
        with canvas.engine.event_source(other.interaction_id):
            assert canvas.engine.undo()
        assert canvas.current_page_id == submap and canvas.node_items[root].detail_page_control.isVisible()
        with canvas.engine.event_source(other.interaction_id):
            assert canvas.engine.redo()
        assert canvas.current_page_id == submap and canvas.node_items[root].visual.width == baseline
    finally:
        other.dispose()


def test_unrelated_navigation_and_deleted_origin_do_not_reuse_stale_return_target(shared):
    """重新从主图进入会覆盖旧来源；子图删除后详情仍能按真实归属返回。"""
    canvas, owner, submap, root = shared
    detail = canvas.engine.create_detail_page(owner)
    canvas.navigate_to(submap)
    canvas.enter_detail_page(root)
    canvas.navigate_to(None, owner)
    canvas.enter_detail_page(owner)
    canvas.return_to_parent_page()
    assert canvas.current_page_id is None
    canvas.navigate_to(submap)
    canvas.enter_detail_page(root)
    canvas.engine.remove_submap(owner)
    assert canvas.current_page_id == detail
    assert canvas.path_navigation.back_button.toolTip() == "返回主导图"
    canvas.return_to_parent_page()
    assert canvas.current_page_id is None


@pytest.mark.parametrize("theme", [Theme(), Theme.dark()])
def test_shared_icon_survives_edit_preview_and_input_composition(shared, theme):
    """明暗主题根图标使用正文对比色，草稿及输入法候选保留同一按钮区。"""
    canvas, owner, submap, root = shared
    canvas.engine.create_detail_page(owner)
    canvas.engine.set_theme(theme, page_id=submap)
    canvas.navigate_to(submap)
    visual = canvas.node_items[root].visual
    base = VisualFactory().visual(canvas.document_view.root, theme, canvas.document_view, is_root=True)
    assert visual.width == base.width + 40 and visual.page_kinds == ("detail",)
    assert visual.page_icon_color == visual.text_color
    canvas.begin_edit(root)
    canvas.editor.setPlainText("Rust 所有权与生命周期\n借用规则")
    canvas.scene.sendEvent(canvas.editor, QInputMethodEvent("中文候选", []))
    control = canvas.node_items[root].detail_page_control
    assert control.isVisible() and not control.can_activate()
    assert canvas.editor.preview_visual.page_kinds == ("detail",)
    assert control.boundingRect() == canvas.editor.preview_visual.page_control_rect("detail")
    with pytest.raises(MindMapError, match="候选"):
        canvas.enter_detail_page(root)
    assert canvas.current_page_id == submap
    canvas.finish_edit(commit=False)
    assert canvas.node_items[root].visual is visual and control.can_activate()


@pytest.mark.parametrize("whole_document", [False, True])
def test_layout_validation_accounts_for_shared_icon_before_committing(shared, whole_document):
    """单页试算和完整文件装载都使用真实共享宽度，布局拒绝不能留下内容提交。"""
    canvas, owner, submap, root = shared
    canvas.engine.create_detail_page(owner)
    canvas.navigate_to(submap)
    limit = canvas.node_items[root].visual.width - 1

    class LimitedWidthLayout:
        info = LayoutInfo("limited-width", "检查共享详情宽度", LayoutConfig(algorithm_id="limited-width"))

        def validate_config(self, config):
            pass

        def layout(self, document, measurements=None, **options):
            width, height = measurements[document.root_id]
            if width > limit:
                raise MindMapError("节点宽度超限")
            rect = Rect(0, 0, width, height)
            return LayoutResult(nodes={document.root_id: rect}, bounds=rect)

    canvas.layout_registry.register(LimitedWidthLayout())
    config = LayoutConfig(algorithm_id="limited-width")
    before, revision = canvas.engine.document, canvas.engine.revision
    with pytest.raises(MindMapError, match="宽度超限"):
        if whole_document:
            candidate = canvas.engine.document
            candidate.submaps[submap].layout = config
            canvas.validate_document(candidate)
        else:
            canvas.set_layout(config)
    assert canvas.engine.document == before and canvas.engine.revision == revision


@pytest.mark.parametrize("suffix", ["png", "svg", "pdf"])
def test_graphic_export_and_saved_document_keep_shared_detail_without_copying(shared, tmp_path, monkeypatch, suffix):
    """三种实际图形导出包含共享图标，保存重开仅保留一个真实详情归属。"""
    canvas, owner, submap, root = shared
    detail = canvas.engine.create_detail_page(owner)
    canvas.navigate_to(submap)
    calls = []
    original = rendering.paint_page_icon

    def paint(painter, kind, rect, color):
        calls.append(kind)
        return original(painter, kind, rect, color)

    monkeypatch.setattr(rendering, "paint_page_icon", paint)
    graphic = tmp_path / f"共享详情.{suffix}"
    canvas.export(graphic)
    assert "detail" in calls and graphic.stat().st_size > 100
    path = tmp_path / "共享详情.mindmap"
    save_document(canvas.engine.document, path)
    restored = MindMapEngine(load_document(path))
    assert len(restored.document.detail_pages) == 1
    assert restored.resolve_detail_page(root) == (owner, detail)
    assert restored.get_node(root).detail_page_id is None
    canvas.enter_detail_page(root)
    state = canvas.capture_page_view_state()
    assert "detail_origin" not in state
    canvas.restore_page_view_state(state)
    assert canvas.path_navigation.back_button.toolTip() == "返回主导图"
