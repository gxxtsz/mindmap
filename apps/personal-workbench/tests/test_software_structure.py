"""通过产品入口验收软件结构文档的浏览、恢复和只读行为。"""

from pathlib import Path

import pytest
from mindmap_core.formats import load_document
from personal_workbench.app import build_window
from PySide6.QtCore import QLineF, QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsSimpleTextItem,
)
from workbench_api import SavePolicy


@pytest.fixture
def window(qtbot, tmp_path):
    workbench = build_window(storage_dir=tmp_path / "配置", restore=False)
    qtbot.addWidget(workbench, before_close_func=lambda item: item.dispose())
    workbench.resize(1140, 740)
    workbench.show()
    return workbench


@pytest.fixture
def graph_path(tmp_path):
    root = Path(__file__).resolve().parents[3]
    example = root / "plugins/workbench-plugin-software-structure/examples/示例.software-structure"
    path = tmp_path / "软件结构示例.software-structure"
    path.write_bytes(example.read_bytes())
    return path


def test_open_dialog_recent_files_and_repeat_open(window, graph_path, monkeypatch):
    def choose_file(parent, title, directory, filters):
        supported, *formats = filters.split(";;")
        assert "*.software-structure" in supported and "软件结构 (*.software-structure)" in formats
        return str(graph_path), ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", choose_file)
    assert window.open_dialog()
    view = window.documents.active
    assert view.document.save_policy == SavePolicy.READ_ONLY
    assert view.document.path == graph_path
    assert window.open_path(graph_path.parent / "." / graph_path.name)
    assert list(window.documents.views.values()) == [view]
    assert window.provider_for("系统.SOFTWARE-STRUCTURE") == "software-structure.document"
    assert window.state.recent_files == [str(graph_path)]

    assert window.manager.close_document(view.id)
    menu = window.recent_files_button.menu()
    menu.aboutToShow.emit()
    action = next(action for action in menu.actions() if action.data() == str(graph_path))
    action.trigger()
    assert window.documents.active.document.path == graph_path


def test_invalid_file_reports_field_without_creating_a_tab(window, graph_path, tmp_path):
    assert window.open_path(graph_path)
    existing = window.documents.active
    invalid = tmp_path / "坏引用.software-structure"
    invalid.write_text(
        '{"schema_version":1,"modules":[{"id":"app","name":"应用"}],'
        '"module_dependencies":[{"source":"app","target":"missing"}]}', "utf-8",
    )
    assert window.call(lambda: window.open_path(invalid)) is None
    message = "module_dependencies[0].target 引用了未声明的模块 ID"
    assert message in window.status.currentMessage()
    assert message in window.operation_log.plain_text()
    assert list(window.documents.views.values()) == [existing]
    assert window.state.recent_files == [str(graph_path)]


def test_graph_shows_modules_arrows_cycles_and_theme(window, graph_path):
    assert window.open_path(graph_path)
    canvas = window.documents.active.widget
    items = canvas.scene().items()
    assert {item.text() for item in items if isinstance(item, QGraphicsSimpleTextItem)} == {
        "应用入口", "订单服务", "商品服务", "支付适配", "账本模块", "数据存储", "缓存模块", "巡检工具",
    }
    arrow_tips = [item.polygon()[0] for item in items if isinstance(item, QGraphicsPolygonItem)]
    assert len(arrow_tips) == len({(point.x(), point.y()) for point in arrow_tips}) == 9
    cycles = [item for item in items if isinstance(item, QGraphicsPathItem)
              and item.pen().style() == Qt.PenStyle.DashLine]
    assert len(cycles) == 3
    assert any("cache → cache" in item.toolTip() for item in cycles)
    assert any("ID：ledger" in item.toolTip() and "记录付款流水" in item.toolTip() for item in items)

    for theme in ("dark", "light"):
        window.manager.set_theme(theme)
        assert canvas.backgroundBrush().color() == window.manager.theme.color("editor-bg")
        assert all(item.pen().color() == window.manager.theme.color("warning") for item in cycles)


def test_empty_graph_has_an_empty_message(window, tmp_path):
    path = tmp_path / "空.software-structure"
    path.write_text('{"schema_version":1,"modules":[]}', "utf-8")
    assert window.open_path(path)
    view = window.documents.active
    assert view.widget.accessibleDescription() == "文件中暂无模块"
    assert [item.text() for item in view.widget.scene().items()] == ["文件中暂无模块"]
    assert not view.toolbar_actions[0].isEnabled()


def test_viewport_interactions_duplicate_and_restart(window, graph_path, qtbot, tmp_path):
    original_bytes = graph_path.read_bytes()
    assert window.open_path(graph_path)
    first = window.documents.active
    canvas = first.widget
    qtbot.waitUntil(lambda: bool(first.capture_state()))
    fitted = first.capture_state()

    viewport = canvas.viewport()
    anchor = viewport.rect().center() + QPoint(28, 18)
    scene_anchor = canvas.mapToScene(anchor)
    event = QWheelEvent(QPointF(anchor), QPointF(viewport.mapToGlobal(anchor)), QPoint(), QPoint(0, 120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False)
    QApplication.sendEvent(viewport, event)
    zoomed = first.capture_state()
    assert zoomed["zoom"] > fitted["zoom"]
    assert QLineF(scene_anchor, canvas.mapToScene(anchor)).length() < 2

    start = QPoint(48, 64)
    end = start + QPoint(62, 37)
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(viewport, end)
    qtbot.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)
    panned = first.capture_state()
    assert panned["center"][0] < zoomed["center"][0] - 20
    assert panned["center"][1] < zoomed["center"][1] - 10

    assert window.manager.duplicate_document(first.id)
    copied = window.documents.active
    assert copied.document is first.document and copied.widget is not canvas
    qtbot.waitUntil(lambda: copied.widget.transform().m11() == pytest.approx(panned["zoom"]))
    assert copied.capture_state()["center"] == pytest.approx(panned["center"], abs=2)
    copied.toolbar_actions[0].trigger()
    assert copied.capture_state()["zoom"] == pytest.approx(fitted["zoom"])
    assert first.capture_state()["center"] == pytest.approx(panned["center"], abs=2)
    states = {item.id: item.capture_state() for item in (first, copied)}
    assert window.state.recent_files == [str(graph_path)]
    assert window.close()

    restored = build_window(storage_dir=tmp_path / "配置")
    qtbot.addWidget(restored, before_close_func=lambda item: item.dispose())
    restored.show()
    assert restored.documents.active.id == copied.id
    assert restored.documents.views[first.id].document is restored.documents.views[copied.id].document
    assert restored.state.recent_files == [str(graph_path)]
    for view_id, state in states.items():
        restored.documents.activate_view(view_id)
        view = restored.documents.active
        qtbot.waitUntil(lambda: view.widget.transform().m11() == pytest.approx(state["zoom"]))
        assert view.capture_state()["center"] == pytest.approx(state["center"], abs=2)
    view.restore_state({"version": 1, "zoom": float("nan"), "center": [0, 0]})
    assert view.capture_state()["zoom"] == pytest.approx(fitted["zoom"])
    assert restored.close()
    assert graph_path.read_bytes() == original_bytes


def test_read_only_actions_and_mindmap_saving(window, graph_path, tmp_path, monkeypatch, qtbot):
    bar = window.title_bar.menu_bar
    entry = bar.actions()[0]
    files = entry.menu()

    def open_file_menu():
        qtbot.mouseClick(bar, Qt.MouseButton.LeftButton, pos=bar.actionGeometry(entry).center())
        qtbot.waitUntil(files.isVisible)

    open_file_menu()
    assert window.action("app.new") in files.actions()
    files.close()
    assert window.open_path(graph_path)
    graph = window.documents.active
    monkeypatch.setattr(graph.document, "save", lambda **kwargs: pytest.fail("只读文档不应进入宿主保存流程"))
    monkeypatch.setattr(window, "confirm_close", lambda view: pytest.fail("只读文档不应弹出保存对话框"))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: pytest.fail("不应打开新建导图对话框"))
    for action_id in ("app.save", "app.save_as", "app.save_all"):
        assert not window.action(action_id).isEnabled()
    assert not window.save() and not window.save(save_as=True)
    assert not window.new_document(graph_path)
    assert not window.new_document()

    open_file_menu()
    assert window.action("app.new") not in files.actions()
    assert window.action("app.open") in files.actions() and window.action("app.open").isEnabled()
    assert all(action.text() != "导出当前页" for action in files.actions())
    files.close()
    assert not window.action("app.new").isEnabled()
    qtbot.keyClick(graph.widget, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
    assert list(window.documents.views.values()) == [graph]

    target = tmp_path / "一起打开的导图.mindmap"
    assert window.new_document(target)
    mindmap = window.documents.active
    assert mindmap.document.path == target
    assert window.action("app.new").isEnabled()
    open_file_menu()
    assert window.action("app.new") in files.actions() and window.action("app.new").text() == "新建导图"
    assert any(action.text() == "导出当前页" for action in files.actions())
    files.close()
    mindmap.engine.update_node(mindmap.engine.root_id, text="批量保存的内容")
    mindmap.document.timer.stop()
    window.documents.activate_view(graph.id)
    assert not window.action("app.save").isEnabled()
    assert window.action("app.save_all").isEnabled()
    assert window.save_all()
    assert load_document(target).root.text == "批量保存的内容"
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(target), ""))
    window.action("app.open").trigger()
    assert window.documents.active is mindmap
    assert window.manager.close_document(graph.id)
