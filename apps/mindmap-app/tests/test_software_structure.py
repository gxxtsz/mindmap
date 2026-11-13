"""节点架构页面保留的图形、主题、鼠标手势和极小倍率回归。"""

import json
from pathlib import Path

import pytest
from mindmap_app.app import build_window
from PySide6.QtCore import QEvent, QLineF, QPoint, QPointF, Qt
from PySide6.QtGui import QFocusEvent, QFont, QMouseEvent, QPainterPathStroker
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsSimpleTextItem,
    QToolButton,
)


def _active_page(window):
    """借用活动导图节点内的软件架构页，测试不再注册独立文档。"""
    return window.documents.active.pages._page


def _open_graph(window, path):
    """以真实节点导入流程建立待测画布，来源文件只读。"""
    assert window.new_document(path.with_suffix('.mindmap'))
    view = window.documents.active
    assert view.pages.open_page(view.engine.root_id, 'software-structure', create=True)
    _active_page(window).import_file(path)
    QApplication.processEvents()
    return True


def _status_controls(window):
    """页面内部的状态工具只跟随本页画布，不使用顶级状态栏。"""
    controls = _active_page(window).controls
    return controls, controls.findChild(QToolButton, 'status_fit'), controls.findChild(QToolButton, 'status_reset_zoom')


@pytest.fixture
def window(qtbot, tmp_path):
    desktop = build_window(storage_dir=tmp_path / "配置", restore=False)
    qtbot.addWidget(desktop, before_close_func=lambda item: item.dispose())
    desktop.resize(1140, 740)
    desktop.show()
    return desktop


@pytest.fixture
def graph_path(tmp_path):
    """将包含循环依赖、自依赖和孤立模块的测试图复制到每个用例的独立目录。"""
    fixture_path = Path(__file__).parent / "data" / "示例.software-structure"
    path = tmp_path / "软件结构示例.software-structure"
    path.write_bytes(fixture_path.read_bytes())
    return path


def _dependency_items(canvas, *, visible_only=True):
    """按依赖提示配对线条和箭头，默认只返回可见依赖。"""
    items = canvas.scene().items()
    arrows = {item.toolTip(): item for item in items if isinstance(item, QGraphicsPolygonItem)}
    dependencies = {}
    for line in items:
        if isinstance(line, QGraphicsPathItem) and (arrow := arrows.get(line.toolTip())) is not None:
            assert line.isVisible() == arrow.isVisible()
            if not visible_only or line.isVisible():
                name = line.toolTip().split("：", 1)[1].removesuffix("</qt>")
                dependencies[name] = line, arrow
    return dependencies


def _highlighted_dependencies(canvas):
    """读取使用加粗线条突出显示的依赖。"""
    return {name for name, (line, _) in _dependency_items(canvas).items() if line.pen().widthF() > 2}


def _module_label(canvas, title):
    """通过画布中实际显示的标题定位模块卡片。"""
    return next(item for item in canvas.scene().items()
                if isinstance(item, QGraphicsSimpleTextItem) and item.text() == title)


def _click_module_title(qtbot, canvas, title):
    """通过真实视口点击模块标题，返回相应卡片以校验选中样式。"""
    label = _module_label(canvas, title)
    position = canvas.mapFromScene(label.sceneBoundingRect().center())
    qtbot.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton, pos=position)
    return label.parentItem()


def _send_canvas_mouse(canvas, kind, position, *, button=Qt.MouseButton.NoButton, buttons=Qt.MouseButton.NoButton):
    """通过真实视口路由派发鼠标事件，显式携带当前按下的按键。"""
    viewport = canvas.viewport()
    event = QMouseEvent(kind, QPointF(position), QPointF(viewport.mapToGlobal(position)),
                        button, buttons, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(viewport, event)


def _visible_center(canvas):
    """将视口的几何中心映射到场景坐标，避免低倍率下先对中心像素取整。"""
    viewport = canvas.viewport()
    point = QPointF(viewport.width() / 2, viewport.height() / 2)
    return canvas.viewportTransform().inverted()[0].map(point)


def test_graph_shows_complete_titles_with_fractional_widths(window, tmp_path, monkeypatch):
    """验证能放入卡片的混排标题在字宽带小数时仍显示完整。"""
    from mindmap_extension_software_structure.module_deps.canvas import ModuleDependencyCanvas

    # 以小数字间距复现绘制宽度高于整数度量的情况。
    font = window.font()
    font.setPixelSize(12)
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.75)
    monkeypatch.setattr(ModuleDependencyCanvas, "font", lambda _: font)
    names = {"sim": "仿真核心（gz-sim 10）", "rendering": "渲染引擎接口（gz-rendering 10）"}
    path = tmp_path / "标题宽度.software-structure"
    path.write_text(json.dumps({
        "schema_version": 1,
        "modules": [{"id": module_id, "name": name} for module_id, name in names.items()],
    }), encoding="utf-8")
    assert _open_graph(window, path)
    items = _active_page(window).widget.scene().items()
    assert {item.text() for item in items if isinstance(item, QGraphicsSimpleTextItem)} == set(names.values())


def test_module_selection_highlights_direct_dependencies_in_each_theme(window, graph_path, qtbot):
    """以卡片和标题点选模块，核对实际线条、箭头和两套主题的选中样式。"""
    assert _open_graph(window, graph_path)
    view = _active_page(window)
    canvas = view.widget
    qtbot.waitUntil(lambda: "zoom" in view.widget.capture_viewport())
    scene, viewport_state = canvas.scene(), view.widget.capture_viewport()
    orders = _click_module_title(qtbot, canvas, "订单服务")
    assert _highlighted_dependencies(canvas) == {"app → orders", "orders → billing", "orders → storage"}
    opened_state = view.widget.capture_viewport()
    assert canvas.scene() is scene and opened_state["zoom"] == viewport_state["zoom"]
    # 首次打开侧栏会缩小画布。场景中心只允许产生 Qt 视口像素取整的偏移。
    assert opened_state["center"] == pytest.approx(viewport_state["center"], abs=3 / canvas.zoom_factor)

    billing = _module_label(canvas, "支付适配").parentItem()
    bounds = billing.sceneBoundingRect()
    body = canvas.mapFromScene(QPointF(bounds.left() + 6, bounds.center().y()))
    assert canvas.itemAt(body) is billing
    qtbot.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton, pos=body)
    highlighted = {"orders → billing", "billing → ledger", "ledger → billing"}
    cycles = {"billing → ledger", "ledger → billing", "cache → cache"}
    items = _dependency_items(canvas)
    _, unrelated_arrow = items["app → catalog"]
    for theme in ("light", "dark"):
        window.manager.set_theme(theme)
        color = window.manager.theme.color
        assert _highlighted_dependencies(canvas) == highlighted
        assert billing.pen().color() == color("control-brand-border")
        assert billing.brush().color() == color("toolbar-selected-bg-active")
        assert orders.pen().color() == color("editor-border")
        for dependency, (line, arrow) in items.items():
            pen = line.pen()
            if dependency in cycles:
                assert pen.style() == Qt.PenStyle.DashLine and pen.color() == color("warning")
            else:
                token = "control-brand-border" if dependency in highlighted else "text-muted"
                assert pen.style() == Qt.PenStyle.SolidLine and pen.color() == color(token)
            assert arrow.brush().color() == pen.color()
            if dependency in highlighted:
                assert unrelated_arrow.zValue() < line.zValue() < arrow.zValue() < billing.zValue()
                assert arrow.scale() > 1.0
            else:
                assert arrow.scale() == 1.0

    _click_module_title(qtbot, canvas, "缓存模块")
    assert _highlighted_dependencies(canvas) == {"cache → cache"}
    loop, _ = items["cache → cache"]
    assert loop.pen().style() == Qt.PenStyle.DashLine and loop.pen().color() == color("warning")
    isolated = _click_module_title(qtbot, canvas, "巡检工具")
    assert isolated.brush().color() == color("toolbar-selected-bg-active")
    assert _highlighted_dependencies(canvas) == set()
    blank = QPoint(8, 8)
    assert canvas.itemAt(blank) is None
    qtbot.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton, pos=blank)
    assert isolated.pen().color() == color("editor-border")
    assert _highlighted_dependencies(canvas) == set()


def test_dashed_dependencies_join_the_arrowhead_when_highlighted(window, tmp_path, qtbot):
    """循环边的虚线末端和放大的箭头保持贴合，包含逆向边和自环。"""
    path = tmp_path / "循环依赖.software-structure"
    edges = (("a", "a"), ("a", "b"), ("b", "c"), ("c", "a"))
    path.write_text(json.dumps({
        "schema_version": 1,
        "modules": [{"id": name, "name": name} for name in "abc"],
        "module_dependencies": [{"source": source, "target": target} for source, target in edges],
    }), encoding="utf-8")
    assert _open_graph(window, path)
    view = _active_page(window)
    qtbot.waitUntil(lambda: "zoom" in view.widget.capture_viewport())
    canvas = view.widget
    dependencies = _dependency_items(canvas)
    assert set(dependencies) == {"a → a", "a → b", "b → c", "c → a"}

    for selected in (False, True):
        if selected:
            _click_module_title(qtbot, canvas, "a")
            assert _highlighted_dependencies(canvas) == {"a → a", "a → b", "c → a"}
        for theme in ("light", "dark"):
            window.manager.set_theme(theme)
            for line, arrow in dependencies.values():
                assert line.pen().style() == Qt.PenStyle.DashLine
                tip, left, right = arrow.polygon()
                base = line.mapFromScene(arrow.mapToScene((left + right) / 2))
                stroke = QPainterPathStroker(line.pen()).createStroke(line.path())
                assert stroke.contains(base)
                assert not stroke.contains(line.mapFromScene(arrow.mapToScene(tip)))


@pytest.mark.parametrize("interruption", ["focus", "capture"])
def test_canvas_drag_from_a_module_ends_on_interruption(window, graph_path, qtbot, interruption):
    """右键超过阈值才平移，左键拖过阈值即取消点击，捕获中断后复位光标。"""
    assert _open_graph(window, graph_path)
    view = _active_page(window)
    canvas = view.widget
    qtbot.waitUntil(lambda: "zoom" in view.widget.capture_viewport())
    _click_module_title(qtbot, canvas, "订单服务")
    highlighted = _highlighted_dependencies(canvas)
    label = _module_label(canvas, "商品服务")
    start = canvas.mapFromScene(label.sceneBoundingRect().center())
    threshold = QApplication.startDragDistance()
    jitter = start + QPoint(threshold - 1, 0)
    delta = QPoint(threshold + 35, 24)
    end = start + delta
    left, right = Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton
    press, move, release = QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove, QEvent.Type.MouseButtonRelease
    horizontal, vertical = canvas.horizontalScrollBar(), canvas.verticalScrollBar()
    old_x, old_y = horizontal.value(), vertical.value()

    _send_canvas_mouse(canvas, press, start, button=right, buttons=right)
    _send_canvas_mouse(canvas, move, jitter, buttons=right)
    assert canvas.viewport().cursor().shape() != Qt.CursorShape.ClosedHandCursor
    _send_canvas_mouse(canvas, release, jitter, button=right)
    _send_canvas_mouse(canvas, press, start, button=left, buttons=left)
    _send_canvas_mouse(canvas, move, end, buttons=left)
    _send_canvas_mouse(canvas, move, start, buttons=left)
    _send_canvas_mouse(canvas, release, start, button=left)
    assert (horizontal.value(), vertical.value()) == (old_x, old_y)
    assert _highlighted_dependencies(canvas) == highlighted

    _send_canvas_mouse(canvas, press, start, button=right, buttons=right)
    _send_canvas_mouse(canvas, move, end, buttons=right)
    assert canvas.viewport().cursor().shape() == Qt.CursorShape.ClosedHandCursor
    assert (horizontal.value(), vertical.value()) == (old_x - delta.x(), old_y - delta.y())
    _send_canvas_mouse(canvas, press, end, button=left, buttons=left | right)
    _send_canvas_mouse(canvas, release, end, button=left, buttons=right)
    assert canvas.viewport().cursor().shape() == Qt.CursorShape.ClosedHandCursor
    if interruption == "focus":
        QApplication.sendEvent(canvas, QFocusEvent(QEvent.Type.FocusOut))
    else:
        QApplication.sendEvent(canvas.viewport(), QEvent(QEvent.Type.UngrabMouse))
    assert canvas.viewport().cursor().shape() != Qt.CursorShape.ClosedHandCursor
    _send_canvas_mouse(canvas, move, end + QPoint(28, 15), buttons=right)
    _send_canvas_mouse(canvas, release, end + QPoint(28, 15), button=right)
    assert (horizontal.value(), vertical.value()) == (old_x - delta.x(), old_y - delta.y())
    assert _highlighted_dependencies(canvas) == highlighted
    _click_module_title(qtbot, canvas, "商品服务")
    assert _highlighted_dependencies(canvas) == {"app → catalog", "catalog → storage"}


@pytest.mark.parametrize("isolated_modules", [0, 12])
def test_status_zoom_below_one_percent(window, tmp_path, qtbot, isolated_modules):
    """长依赖链适配后显示小于 1%，恢复原始尺寸时按可平移范围保留视口中心。"""
    chain = [f"module-{index}" for index in range(800)]
    identifiers = chain + [f"isolated-{index}" for index in range(isolated_modules)]
    path = tmp_path / "长依赖链.software-structure"
    path.write_text(json.dumps({
        "schema_version": 1,
        "modules": [{"id": identifier, "name": identifier} for identifier in identifiers],
        "module_dependencies": [
            {"source": source, "target": target} for source, target in zip(chain, chain[1:])
        ],
    }), encoding="utf-8")
    assert _open_graph(window, path)
    view = _active_page(window)
    qtbot.waitUntil(lambda: bool(view.widget.capture_viewport()))
    _, _, zoom = _status_controls(window)
    canvas = view.widget
    assert 0 < canvas.zoom_factor < 0.01
    assert zoom.text() == "<1%"
    center = _visible_center(canvas)
    qtbot.mouseClick(zoom, Qt.MouseButton.LeftButton)
    assert canvas.zoom_factor == 1.0 and zoom.text() == "100%"

    # 100% 下从可滚动区域扣除半屏余量，得到可达的视口中心范围。
    width, height = canvas.viewport().size().toTuple()
    bounds = canvas.sceneRect().adjusted(width / 2, height / 2, -width / 2, -height / 2)
    expected = QPointF(
        min(bounds.right(), max(bounds.left(), center.x())),
        min(bounds.bottom(), max(bounds.top(), center.y())),
    )
    assert QLineF(_visible_center(canvas), expected).length() < 2
