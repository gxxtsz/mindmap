"""初始画布倍率、根节点定位及文件替换的生命周期回归测试。"""

import pytest
from mindmap_engine import LayoutConfig, MindMapDocument, MindMapEngine, MindMapError, Node, save_document
from mindmap_qt import MindMapFileController, MindMapPanels, MindMapWidget
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, QRectF
from PySide6.QtWidgets import QApplication, QFileDialog
from shiboken6 import isValid


def sample_document(*, direction="right", children=80, text="初始根主题"):
    """构造超出视口的导图并保留一个折叠分支，避免适应整图也能通过验收。"""
    document = MindMapDocument.new(text)
    document.layout = LayoutConfig(direction=direction)
    for index in range(children):
        child = Node(f"分支 {index}")
        document.nodes[child.id] = child
        document.root.children.append(child.id)
        if index == 0:
            hidden = Node("仍然折叠的后代")
            document.nodes[hidden.id] = hidden
            child.children.append(hidden.id)
            child.expanded = False
    return document


def assert_initial_view(widget):
    """按逻辑像素检查真实屏幕几何，根节点中心允许 Qt 整数滚动产生两像素误差。"""
    view = widget.view
    assert view.transform().isIdentity()
    root = widget.node_items[widget.engine.root_id].sceneBoundingRect()
    offset = view.viewportTransform().map(root.center()) - QRectF(view.viewport().rect()).center()
    assert abs(offset.x()) <= 2 and abs(offset.y()) <= 2
    assert QRectF(view.viewport().rect()).intersects(view.viewportTransform().mapRect(root))


@pytest.fixture
def canvas_factory(qtbot):
    """创建真实画布；测试可在首次显示前加载文件，退出时统一释放引擎订阅。"""
    widgets = []

    def create(document=None, *, register=True):
        widget = MindMapWidget(MindMapEngine(document), toolbar=False)
        widget.resize(1000, 700)
        if register:
            qtbot.addWidget(widget)
        widgets.append(widget)
        return widget

    yield create
    for widget in widgets:
        if isValid(widget):
            widget.dispose()


@pytest.fixture
def files(canvas_factory, qtbot):
    """文件控制器使用实际面板组合，确保替换通知及选择同步走正式路径。"""
    widget = canvas_factory(sample_document())
    panels = MindMapPanels(widget, widget)
    controller = MindMapFileController(widget, panels, widget)
    widget.view.scale(0.4, 0.4)
    widget.show()
    qtbot.waitUntil(lambda: assert_initial_view(widget))
    yield controller
    panels.dispose()


@pytest.mark.parametrize("direction", ["left", "right", "both", "down"])
@pytest.mark.parametrize("children", [0, 80])
def test_first_show_uses_actual_size_and_original_scale(canvas_factory, qtbot, direction, children):
    """隐藏时不初始化；大小导图和四种方向首次显示均以根节点为中心使用 100%。"""
    document = sample_document(direction=direction, children=children)
    widget = canvas_factory(document)
    widget.view.scale(0.4, 0.4)
    QApplication.processEvents()
    assert widget.view.transform().m11() == pytest.approx(0.4)
    widget.resize(1150, 780)
    widget.show()
    qtbot.waitUntil(lambda: assert_initial_view(widget))
    assert widget.engine.document == document
    assert not widget.engine.is_dirty and not widget.engine.can_undo and not widget.engine.can_redo
    assert widget.selected_node_ids() == [document.root_id]


@pytest.mark.parametrize("operation", ["new", "open"])
def test_file_replacement_resets_old_zoom_and_focus(files, qtbot, tmp_path, operation):
    """新建和打开都摆脱旧倍率及分支聚焦，不产生额外历史或文档修改。"""
    widget = files.canvas
    widget.focus_node(widget.engine.document.root.children[-1])
    widget.view.zoom(0.4 / widget.view.transform().m11())
    source = tmp_path / "新打开.mindmap"
    document = sample_document(direction="left", text="新文档根主题")
    save_document(document, source)
    original = source.read_bytes()
    if operation == "open":
        assert files.open_path(source)
        assert widget.engine.document == document and files.path == source
    else:
        assert files.new_document()
        assert widget.engine.document.root.text == "中心主题" and files.path is None
    qtbot.waitUntil(lambda: assert_initial_view(widget))
    assert widget.focus_id is None
    assert not widget.engine.is_dirty and not widget.engine.can_undo and not widget.engine.can_redo
    assert source.read_bytes() == original


@pytest.mark.parametrize("operation", ["cancel_open", "cancel_new", "cancel_dialog", "invalid"])
def test_unsuccessful_file_change_keeps_view(files, qtbot, tmp_path, monkeypatch, operation):
    """取消或解码失败不得排程视图重置；再次运行事件循环后仍保持倍率、位置和文档。"""
    widget, view = files.canvas, files.canvas.view
    view.zoom(0.7)
    view.centerOn(widget.node_items[widget.engine.root_id].sceneBoundingRect().center() + QPointF(90, -60))
    transform = view.transform()
    center = view.mapToScene(view.viewport().rect()).boundingRect().center()
    document, revision = widget.engine.document, widget.engine.revision
    source = tmp_path / "候选.mindmap"
    save_document(sample_document(text="另一个根主题"), source)
    if operation == "invalid":
        source.write_bytes(b"invalid mindmap")
        with pytest.raises(MindMapError):
            files.open_path(source)
    elif operation == "cancel_dialog":
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: ("", ""))
        assert not files.open_dialog()
    else:
        monkeypatch.setattr(files, "confirm_replace", lambda: False)
        assert not (files.new_document() if operation == "cancel_new" else files.open_path(source))
    QApplication.processEvents()
    assert view.transform() == transform
    assert view.mapToScene(view.viewport().rect()).boundingRect().center() == center
    assert widget.engine.document == document and widget.engine.revision == revision


def test_hidden_replacements_initialize_only_the_latest_document(files, qtbot, tmp_path):
    """隐藏标签连续替换文档时不使用占位尺寸，重新可见后定位最新文档的根节点。"""
    widget = files.canvas
    widget.view.zoom(0.4)
    widget.hide()
    for text in ("已被替换的根", "最终根"):
        source = tmp_path / (text + ".mindmap")
        document = sample_document(text=text)
        save_document(document, source)
        assert files.open_path(source)
        QApplication.processEvents()
        assert widget.view.transform().m11() == pytest.approx(0.4)
    widget.resize(800, 600)
    widget.show()
    qtbot.waitUntil(lambda: assert_initial_view(widget))
    assert widget.engine.document == document


def test_completed_initialization_preserves_later_navigation(files):
    """首次定位完成后，显示、重排和正常刷新均不能覆盖用户已经调整的视图。"""
    widget, view = files.canvas, files.canvas.view
    view.zoom(0.7)
    view.centerOn(widget.node_items[widget.engine.root_id].sceneBoundingRect().center() + QPointF(90, -60))
    transform = view.transform()
    center = view.mapToScene(view.viewport().rect()).boundingRect().center()
    document, revision = widget.engine.document, widget.engine.revision
    widget.hide()
    widget.resize(1200, 800)
    widget.show()
    QApplication.processEvents()
    widget.refresh()
    QApplication.processEvents()
    assert view.transform() == transform
    actual = view.mapToScene(view.viewport().rect()).boundingRect().center()
    assert (actual - center).manhattanLength() < 2 / transform.m11()
    assert widget.engine.document == document and widget.engine.revision == revision


def test_explicit_reset_only_changes_the_view(files, qtbot):
    """公开重置接口可以重复调用；保留用户选择、脏标记及已有撤销和重做记录。"""
    widget = files.canvas
    child = widget.engine.document.root.children[-1]
    widget.engine.update_node(child, text="已经提交的修改")
    widget.engine.add_child(child, "可重做的后代")
    assert widget.engine.undo()
    widget.select_nodes([child])
    document, revision = widget.engine.document, widget.engine.revision
    widget.view.zoom(0.4)
    for _ in range(3):
        widget.reset_view()
    qtbot.waitUntil(lambda: assert_initial_view(widget))
    assert widget.selected_node_ids() == [child]
    assert widget.engine.document == document and widget.engine.revision == revision
    assert widget.engine.is_dirty and widget.engine.can_undo and widget.engine.can_redo


@pytest.mark.parametrize("delete", [False, True])
def test_dispose_cancels_queued_initialization(canvas_factory, delete):
    """显示后立即释放时，排队定位不能触碰已释放或已经被 Qt 删除的组件。"""
    # 删除用例自行拥有并销毁控件，避免 qtbot 在清理阶段再次 close 已删除的对象。
    widget = canvas_factory(sample_document(), register=not delete)
    widget.view.scale(0.4, 0.4)
    widget.show()
    widget.reset_view()
    widget.dispose()
    if delete:
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()
    if delete:
        assert not isValid(widget)
    else:
        assert widget.view.transform().m11() == pytest.approx(0.4)
        widget.reset_view()
        QApplication.processEvents()
        assert widget.view.transform().m11() == pytest.approx(0.4)
