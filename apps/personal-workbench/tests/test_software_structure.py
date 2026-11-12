"""通过产品入口验收软件结构文档的浏览、恢复和只读行为。"""

import json
from pathlib import Path

import pytest
from mindmap_core.formats import load_document
from personal_workbench.app import build_window
from PySide6.QtCore import QEvent, QLineF, QPoint, QPointF, QRect, QSaveFile, QStandardPaths, Qt, QTimer
from PySide6.QtGui import QFocusEvent, QFont, QIcon, QMouseEvent, QPainterPathStroker, QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsSimpleTextItem,
    QListWidget,
    QMessageBox,
    QStyle,
    QStyleOptionViewItem,
    QToolButton,
    QWidget,
)
from workbench_api import SavePolicy
from workbench_runtime.ui.docking import DockPosition, DockTarget

_DOWNLOAD_ACTION_ID = "software-structure.download-template"
_DEPENDENCY_FILTER_PANEL_ID = "software-structure.public-dependencies"


@pytest.fixture
def window(qtbot, tmp_path):
    workbench = build_window(storage_dir=tmp_path / "配置", restore=False)
    qtbot.addWidget(workbench, before_close_func=lambda item: item.dispose())
    workbench.resize(1140, 740)
    workbench.show()
    return workbench


@pytest.fixture
def graph_path(tmp_path):
    """将包含循环依赖、自依赖和孤立模块的测试图复制到每个用例的独立目录。"""
    fixture_path = Path(__file__).parent / "data" / "示例.software-structure"
    path = tmp_path / "软件结构示例.software-structure"
    path.write_bytes(fixture_path.read_bytes())
    return path


@pytest.fixture
def legacy_filter_graph_path(tmp_path):
    """提供含旧公共依赖勾选和扩展字段的文件，验证按原勾选筛选及迁移。"""
    ordinary = [f"module-{index}" for index in range(9)]
    pairs = [*zip(ordinary, ordinary[1:]),
             *((module_id, "shared-a") for module_id in ordinary[:7]),
             *((module_id, "shared-b") for module_id in ordinary[:6])]
    path = tmp_path / "旧版筛选.software-structure"
    path.write_text(json.dumps({
        "schema_version": 1,
        "modules": [
            *({"id": name, "name": name} for name in ordinary),
            {"id": "shared-a", "name": "shared-a", "is_public_dependency": True},
            {"id": "shared-b", "name": "shared-b"},
        ],
        "module_dependencies": [{"source": source, "target": target} for source, target in pairs],
        "public_dependency_thresholds": {"component_size": 10, "direct_dependent_percent": 50},
        "apis": [{"module_id": "shared-a", "path": "/world"}],
    }, indent=2), encoding="utf-8-sig")
    return path


def _status_controls(window):
    """在窗口状态栏中定位软件结构插件的实际按钮。"""
    controls = window.status.findChild(QWidget, "software_structure_status_controls")
    assert controls is not None
    fit = controls.findChild(QToolButton, "status_fit")
    zoom = controls.findChild(QToolButton, "status_reset_zoom")
    assert fit is not None and zoom is not None
    return controls, fit, zoom


def _dependency_filter_list(window):
    """定位已展开的连线筛选面板和模块列表。"""
    panel = window.manager.panel(_DEPENDENCY_FILTER_PANEL_ID).content
    modules = panel.findChild(QListWidget, "dependency_filter_modules")
    assert modules is not None
    return panel, modules


def _dependency_filter_header_button(window):
    """取得宿主为连线筛选侧栏创建的标题栏按钮。"""
    button, = window.manager.panel(_DEPENDENCY_FILTER_PANEL_ID).action_buttons
    return button


def _status_filter_button(window):
    """取得状态栏中用于临时查看全部连线的实际漏斗按钮。"""
    controls, _, _ = _status_controls(window)
    button = controls.findChild(QToolButton, "status_dependency_filter")
    assert button is not None
    return button


def _dependency_filter_checks(modules):
    """读取侧栏按文件顺序显示的勾选状态。"""
    return [modules.item(row).checkState() for row in range(modules.count())]


def _dependency_filter_item(modules, module_id):
    """按文件里的原始模块 ID 取得列表项。"""
    return next(item for row in range(modules.count())
                if (item := modules.item(row)).data(Qt.ItemDataRole.UserRole) == module_id)


def _click_dependency_filter_checkbox(qtbot, modules, item):
    """定位当前 Qt 样式的勾选区域，通过列表视口点击。"""
    modules.scrollToItem(item)
    index = modules.indexFromItem(item)
    option = QStyleOptionViewItem()
    modules.initViewItemOption(option)
    modules.itemDelegateForIndex(index).initStyleOption(option, index)
    option.rect = modules.visualItemRect(item)
    indicator = modules.style().subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator, option, modules)
    assert indicator.isValid()
    qtbot.mouseClick(modules.viewport(), Qt.MouseButton.LeftButton, pos=indicator.center())


def _dependency_arrow_count(view):
    """按场景中实际绘制的箭头统计可见依赖。"""
    return sum(isinstance(item, QGraphicsPolygonItem) and item.isVisible() for item in view.widget.scene().items())


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


def test_open_dialog_recent_files_and_repeat_open(window, graph_path, monkeypatch):
    def choose_file(parent, title, directory, filters):
        supported, *formats = filters.split(";;")
        assert "*.software-structure" in supported and "软件结构 (*.software-structure)" in formats
        return str(graph_path), ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", choose_file)
    controls, _, _ = _status_controls(window)
    assert not controls.isVisible()
    assert window.open_dialog()
    assert controls.isVisible()
    view = window.documents.active
    assert view.document.save_policy == SavePolicy.READ_ONLY
    assert view.document.path == graph_path
    assert window.open_path(graph_path.parent / "." / graph_path.name)
    assert list(window.documents.views.values()) == [view]
    assert window.provider_for("系统.SOFTWARE-STRUCTURE") == "software-structure.document"
    assert window.state.recent_files == [str(graph_path)]

    assert window.manager.close_document(view.id)
    assert not controls.isVisible()
    menu = window.recent_files_button.menu()
    menu.aboutToShow.emit()
    action = next(action for action in menu.actions() if action.data() == str(graph_path))
    action.trigger()
    assert window.documents.active.document.path == graph_path


def test_download_template_from_file_menu_and_open(window, tmp_path, monkeypatch, qtbot):
    """从无文档的工作台下载模板，再通过正常打开入口查看示例依赖。"""
    target = tmp_path / "生成用模板.software-structure"

    def downloads(location):
        assert location == QStandardPaths.StandardLocation.DownloadLocation
        return str(tmp_path)

    monkeypatch.setattr(QStandardPaths, "writableLocation", downloads)

    def choose_file(dialog):
        assert dialog.parentWidget() is window
        assert Path(dialog.directory().absolutePath()) == tmp_path
        assert Path(dialog.selectedFiles()[0]).name == "软件结构模板.software-structure"
        dialog.selectFile(target.stem)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QFileDialog, "exec", choose_file)
    assert window.documents.active is None
    bar = window.title_bar.menu_bar
    file_entry = next(action for action in bar.actions() if action.text() == "文件")
    menu = file_entry.menu()
    qtbot.mouseClick(bar, Qt.MouseButton.LeftButton, pos=bar.actionGeometry(file_entry).center())
    qtbot.waitUntil(menu.isVisible)
    download = next(action for action in menu.actions() if action.text() == "下载软件结构模板…")
    assert download is window.action(_DOWNLOAD_ACTION_ID)
    menu.setActiveAction(download)
    qtbot.keyClick(menu, Qt.Key.Key_Return)

    instructions = "\n".join(json.loads(target.read_text(encoding="utf-8"))["template_instructions"])
    assert "source 是依赖方，target 是被依赖方" in instructions
    assert "只输出原始 JSON 对象" in instructions
    assert str(target) in window.status.currentMessage()
    assert window.documents.active is None and not window.state.recent_files

    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(target), ""))
    assert window.open_dialog()
    view = window.documents.active
    assert view.document.path == target and view.document.save_policy == SavePolicy.READ_ONLY
    items = view.widget.scene().items()
    assert {item.text() for item in items if isinstance(item, QGraphicsSimpleTextItem)} == {
        "应用入口", "业务服务", "数据存储",
    }
    assert sorted(item.toolTip() for item in items if isinstance(item, QGraphicsPolygonItem)) == [
        "<qt>模块依赖：app → service</qt>", "<qt>模块依赖：service → storage</qt>",
    ]


def test_download_template_from_split_toolbar(window, graph_path, tmp_path, monkeypatch, qtbot):
    """每个分屏的按钮跟随本组标签，点击按钮可保存模板副本。"""
    assert window.open_path(graph_path)
    first = window.documents.active
    assert window.manager.duplicate_document(first.id)
    second = window.documents.active
    window.host.editor.split_token(first.id, Qt.Orientation.Horizontal)
    first_group, _ = window.host.editor.find_token(first.id)
    second_group, _ = window.host.editor.find_token(second.id)
    assert first_group is not second_group
    first_toolbar = first_group.cornerWidget(Qt.Corner.TopRightCorner)
    second_toolbar = second_group.cornerWidget(Qt.Corner.TopRightCorner)
    download = window.action(_DOWNLOAD_ACTION_ID)
    assert first_toolbar.actions() == second_toolbar.actions() == [download]

    window.documents.activate_view(second.id)
    assert window.new_document(tmp_path / "同组导图.mindmap")
    assert window.host.editor.find_token(window.documents.active.id)[0] is second_group
    assert download in first_toolbar.actions() and download not in second_toolbar.actions()
    window.documents.activate_view(second.id)

    target = tmp_path / "从分屏保存.software-structure"

    def choose_file(dialog):
        dialog.selectFile(str(target))
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QFileDialog, "exec", choose_file)
    button = second_toolbar.widgetForAction(download)
    qtbot.waitUntil(button.isVisible)
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert target.is_file()
    assert window.documents.active is second
    assert str(target) not in window.state.recent_files


def test_download_template_confirms_overwrite_after_suffix_completion(window, tmp_path, monkeypatch):
    """用 Qt 的覆盖提示拒绝和接受同名文件，核对提示检查的是补全后的路径。"""
    target = tmp_path / "已有模板.software-structure"
    original = b"original"
    target.write_bytes(original)
    answers = iter((QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes))
    confirmations = []

    def choose_file(dialog):
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog)
        dialog.selectFile(str(target.with_suffix("")))
        answer = next(answers)

        def answer_overwrite():
            prompt = dialog.findChild(QMessageBox)
            if prompt is not None:
                confirmations.append(prompt.text())
                prompt.button(answer).click()

        # accept 会启动确认对话框的事件循环，此时由定时器点击覆盖选项。
        timer = QTimer(dialog)
        timer.setSingleShot(True)
        timer.timeout.connect(answer_overwrite)
        timer.start(0)
        dialog.accept()
        timer.stop()
        return dialog.result()

    monkeypatch.setattr(QFileDialog, "exec", choose_file)
    download = window.action(_DOWNLOAD_ACTION_ID)
    download.trigger()
    assert target.read_bytes() == original
    download.trigger()
    assert json.loads(target.read_text(encoding="utf-8"))["schema_version"] == 1
    assert len(confirmations) == 2 and all(target.name in prompt for prompt in confirmations)


@pytest.mark.parametrize(("filename", "accepted", "message"), [
    ("取消.software-structure", False, None),
    ("扩展名错误.txt", True, "请使用 .software-structure 扩展名保存模板"),
])
def test_download_template_without_valid_destination(window, tmp_path, monkeypatch, filename, accepted, message):
    """取消保存或输入其他后缀时，目标目录中没有模板副本。"""
    original_log = window.operation_log.plain_text()

    def choose_file(dialog):
        dialog.selectFile(str(tmp_path / filename))
        return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected

    monkeypatch.setattr(QFileDialog, "exec", choose_file)
    window.action(_DOWNLOAD_ACTION_ID).trigger()
    assert not any(tmp_path.glob("*.software-structure")) and not (tmp_path / filename).exists()
    if message:
        assert message in window.status.currentMessage() and message in window.operation_log.plain_text()
    else:
        assert window.operation_log.plain_text() == original_log


def test_download_template_write_failure_keeps_existing_file(window, tmp_path, monkeypatch):
    """模拟模板写入中断，原有内容得到保留，宿主同时报告失败。"""
    target = tmp_path / "已有模板.software-structure"
    original = b"original"
    target.write_bytes(original)
    write = QSaveFile.write

    def choose_file(dialog):
        dialog.selectFile(str(target))
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QFileDialog, "exec", choose_file)
    monkeypatch.setattr(QSaveFile, "write", lambda file, content: write(file, content[:1]))
    window.action(_DOWNLOAD_ACTION_ID).trigger()

    assert target.read_bytes() == original
    assert {item for item in tmp_path.iterdir() if item.is_file()} == {target}
    message = "保存软件结构模板失败"
    assert message in window.status.currentMessage() and str(target) in window.status.currentMessage()
    assert message in window.operation_log.plain_text()


def test_file_icon_tracks_theme_and_remains_in_recent_files_after_close(window, graph_path, monkeypatch):
    """真实标签与标题栏使用同一组件图形，关闭后菜单仍按历史文件的扩展名绘制。"""
    assert window.open_path(graph_path)
    view = window.documents.active
    group, index = window.host.editor.find_token(view.id)
    button = window.recent_files_button

    def assert_file_icons():
        theme = window.manager.theme
        icon = theme.icon("software-structure")
        assert not icon.isNull()
        assert group.tabIcon(index).pixmap(14, 14).toImage() == icon.pixmap(14, 14).toImage()
        assert button.icon().pixmap(16, 16).toImage() == icon.pixmap(16, 16).toImage()
        assert button.icon().pixmap(16, 16).toImage() != theme.icon("document").pixmap(16, 16).toImage()

    assert_file_icons()
    window.manager.set_theme("dark")
    assert_file_icons()
    assert window.manager.close_document(view.id)
    theme = window.manager.theme
    assert button.icon().pixmap(16, 16).toImage() == theme.icon("document").pixmap(16, 16).toImage()

    menu = button.menu()
    menu.aboutToShow.emit()
    action = next(action for action in menu.actions() if action.data() == str(graph_path))
    resolve_icon = theme.icon
    painted_icons = []

    def record_icon(name, size=20):
        painted_icons.append((name, size))
        return resolve_icon(name, size)

    monkeypatch.setattr(theme, "icon", record_icon)
    assert not action.defaultWidget().grab().isNull()
    assert ("software-structure", 16) in painted_icons


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
    view = window.documents.active
    canvas = view.widget
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

    group, _ = window.host.editor.find_token(view.id)
    toolbar = group.cornerWidget(Qt.Corner.TopRightCorner)
    download = window.action(_DOWNLOAD_ACTION_ID)
    assert toolbar.actions() == [download]
    download_button = toolbar.widgetForAction(download)
    controls, fit, zoom = _status_controls(window)
    for theme in ("dark", "light"):
        window.manager.set_theme(theme)
        assert canvas.backgroundBrush().color() == window.manager.theme.color("editor-bg")
        assert all(item.pen().color() == window.manager.theme.color("warning") for item in cycles)
        icon = window.manager.theme.icon("export-text")
        assert not icon.isNull()
        assert download_button.icon().pixmap(18, 18).toImage() == icon.pixmap(18, 18).toImage()
        for compact in (True, False):
            window.manager.set_option("compact", compact)
            QApplication.processEvents()
            assert download_button.isVisible() and toolbar.rect().contains(download_button.geometry())
            assert window.status.height() == (24 if compact else 28)
            assert window.status.rect().contains(controls.geometry())
            assert controls.geometry().center().x() > window.status.rect().center().x()
            assert all(button.isVisible() and controls.rect().contains(button.geometry()) for button in (fit, zoom))
            assert fit.x() < zoom.x() and not fit.icon().isNull()


def test_graph_shows_complete_titles_with_fractional_widths(window, tmp_path, monkeypatch):
    """验证能放入卡片的混排标题在字宽带小数时仍显示完整。"""
    from workbench_plugin_software_structure.module_deps.canvas import ModuleDependencyCanvas

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
    assert window.open_path(path)
    items = window.documents.active.widget.scene().items()
    assert {item.text() for item in items if isinstance(item, QGraphicsSimpleTextItem)} == set(names.values())


def test_module_selection_highlights_direct_dependencies_in_each_theme(window, graph_path, qtbot):
    """以卡片和标题点选模块，核对实际线条、箭头和两套主题的选中样式。"""
    assert window.open_path(graph_path)
    view = window.documents.active
    canvas = view.widget
    qtbot.waitUntil(lambda: "zoom" in view.capture_state())
    scene, viewport_state = canvas.scene(), view.capture_state()
    orders = _click_module_title(qtbot, canvas, "订单服务")
    assert _highlighted_dependencies(canvas) == {"app → orders", "orders → billing", "orders → storage"}
    assert canvas.scene() is scene and view.capture_state() == viewport_state

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
    assert window.open_path(path)
    view = window.documents.active
    qtbot.waitUntil(lambda: "zoom" in view.capture_state())
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


def test_edge_filter_hides_incident_lines_without_moving_cards(window, graph_path, qtbot):
    """通过侧栏勾选两端、循环、自依赖及孤立模块，校验原地筛选和选中高亮。"""
    assert window.open_path(graph_path)
    view = window.documents.active
    canvas = view.widget
    assert window.manager.activate(_DEPENDENCY_FILTER_PANEL_ID)
    _, modules = _dependency_filter_list(window)
    filter_button = _status_filter_button(window)
    qtbot.waitUntil(lambda: "zoom" in view.capture_state())
    center = _module_label(canvas, "订单服务").sceneBoundingRect().center()
    view.restore_state({"version": 1, "zoom": 1.25, "center": [center.x() + 20, center.y() + 12]})
    orders = _click_module_title(qtbot, canvas, "订单服务")
    scene, viewport, bounds = canvas.scene(), canvas.viewportTransform(), canvas.sceneRect()
    edges = _dependency_items(canvas)
    assert _highlighted_dependencies(canvas) == {"app → orders", "orders → billing", "orders → storage"}

    def check(module_id):
        _click_dependency_filter_checkbox(qtbot, modules, _dependency_filter_item(modules, module_id))

    check("diagnostics")
    assert filter_button.isHidden() and _dependency_items(canvas) == edges
    check("storage")
    assert filter_button.isVisible() and filter_button.isChecked()
    assert filter_button.toolTip() == filter_button.accessibleName() == "显示全部连线"
    assert _highlighted_dependencies(canvas) == {"app → orders", "orders → billing"}
    check("orders")
    check("storage")
    assert "orders → storage" not in _dependency_items(canvas)
    assert "catalog → storage" in _dependency_items(canvas)
    assert _highlighted_dependencies(canvas) == set()

    check("billing")
    check("cache")
    assert set(_dependency_items(canvas)) == {"app → catalog", "catalog → storage", "ledger → storage"}
    saved = graph_path.read_bytes()
    qtbot.mouseClick(filter_button, Qt.MouseButton.LeftButton)
    assert not filter_button.isChecked() and _dependency_items(canvas) == edges
    assert filter_button.toolTip() == filter_button.accessibleName() == "恢复连线筛选"
    assert _highlighted_dependencies(canvas) == {"app → orders", "orders → billing", "orders → storage"}
    for name in ("billing → ledger", "ledger → billing", "cache → cache"):
        line, arrow = edges[name]
        assert line.pen().style() == Qt.PenStyle.DashLine
        assert arrow.brush().color() == line.pen().color() == window.manager.theme.color("warning")

    filter_button.setFocus()
    qtbot.keyClick(filter_button, Qt.Key.Key_Space)
    assert filter_button.isChecked() and graph_path.read_bytes() == saved
    check("orders")
    assert _highlighted_dependencies(canvas) == {"app → orders", "orders → storage"}
    QApplication.processEvents()
    assert canvas.scene() is scene and canvas.sceneRect() == bounds
    assert canvas.viewportTransform() == viewport and _dependency_items(canvas, visible_only=False) == edges
    assert _module_label(canvas, "订单服务").parentItem() is orders
    assert orders.brush().color() == window.manager.theme.color("toolbar-selected-bg-active")
    assert {item.text() for item in scene.items() if isinstance(item, QGraphicsSimpleTextItem)} == {
        "应用入口", "订单服务", "商品服务", "支付适配", "账本模块", "数据存储", "缓存模块", "巡检工具",
    }


def test_dependency_filter_sidebar_migrates_marks_and_refreshes_splits(window, legacy_filter_graph_path, qtbot):
    """从视图菜单打开侧栏，核对旧勾选、原子迁移和两个分屏的独立预览。"""
    path = legacy_filter_graph_path
    original = path.read_bytes()
    assert not window.manager.is_tool_available(_DEPENDENCY_FILTER_PANEL_ID)
    assert window.open_path(path)
    first = window.documents.active
    assert not window.manager.states[_DEPENDENCY_FILTER_PANEL_ID].visible
    menu = window.main_menu()
    view_menu = next(action.menu() for action in menu.actions() if action.text() == "视图")
    panels = next(action.menu() for action in view_menu.actions() if action.text() == "面板")
    panel_action = next(action for action in panels.actions() if action.text() == "连线筛选")
    assert not panel_action.isChecked()
    panel_action.trigger()
    panel, modules = _dependency_filter_list(window)
    shared_a = _dependency_filter_item(modules, "shared-a")
    shared_b = _dependency_filter_item(modules, "shared-b")
    assert panel.isVisible() and modules.count() == 11
    assert shared_a.checkState() == Qt.CheckState.Checked and shared_b.checkState() == Qt.CheckState.Unchecked
    assert _dependency_arrow_count(first) == 14 and path.read_bytes() == original

    filter_button = _status_filter_button(window)
    qtbot.mouseClick(filter_button, Qt.MouseButton.LeftButton)
    assert _dependency_arrow_count(first) == 21 and not filter_button.isChecked()
    assert window.manager.duplicate_document(first.id)
    second = window.documents.active
    assert second.document is first.document
    assert _dependency_arrow_count(second) == 14 and filter_button.isChecked()
    window.host.editor.split_token(first.id, Qt.Orientation.Horizontal)
    qtbot.waitUntil(lambda: first.widget.isVisible() and second.widget.isVisible())
    window.documents.activate_view(second.id)
    qtbot.mouseClick(filter_button, Qt.MouseButton.LeftButton)
    assert _dependency_arrow_count(second) == 21 and not filter_button.isChecked()
    scene = second.widget.scene()

    with qtbot.waitSignal(first.document.edge_filter_changed):
        _click_dependency_filter_checkbox(qtbot, modules, shared_b)
    expected = json.loads(original)
    del expected["modules"][-2]["is_public_dependency"]
    expected["hidden_edge_module_ids"] = ["shared-a", "shared-b"]
    assert json.loads(path.read_bytes()) == expected
    assert _dependency_arrow_count(first) == 21 and first.widget.shows_all_dependencies
    assert _dependency_arrow_count(second) == 8 and filter_button.isChecked()
    assert second.widget.scene() is scene

    window.documents.activate_view(first.id)
    assert not filter_button.isChecked()
    qtbot.mouseClick(filter_button, Qt.MouseButton.LeftButton)
    assert _dependency_arrow_count(first) == _dependency_arrow_count(second) == 8
    qtbot.mouseClick(filter_button, Qt.MouseButton.LeftButton)
    window.documents.activate_view(second.id)
    _click_dependency_filter_checkbox(qtbot, modules, shared_a)
    assert _dependency_arrow_count(first) == 21 and _dependency_arrow_count(second) == 15
    _click_dependency_filter_checkbox(qtbot, modules, shared_b)
    assert filter_button.isHidden()
    assert _dependency_arrow_count(first) == _dependency_arrow_count(second) == 21
    assert json.loads(path.read_bytes())["hidden_edge_module_ids"] == []
    _click_dependency_filter_checkbox(qtbot, modules, shared_a)
    assert _dependency_arrow_count(first) == 21 and _dependency_arrow_count(second) == 14


def test_dependency_filter_sidebar_checkboxes_and_theme(window, legacy_filter_graph_path, qtbot):
    """首次展开和切换主题时两个筛选图标匹配主题，列表保留键盘焦点。"""
    window.manager.set_theme("dark")
    assert window.open_path(legacy_filter_graph_path)
    assert window.manager.activate(_DEPENDENCY_FILTER_PANEL_ID)
    panel, modules = _dependency_filter_list(window)
    dock = window.manager.panel(_DEPENDENCY_FILTER_PANEL_ID)
    button = _dependency_filter_header_button(window)
    filter_button = _status_filter_button(window)

    def check_sidebar_theme():
        expected = window.manager.theme.color("tool-window-bg")
        # 在标题及内容顶部的留白各采样一处，覆盖带点号的面板标识。
        for y in (6, dock.header.height() + 2):
            sample = dock.grab(QRect(dock.width() // 2, y, 1, 1))
            assert sample.toImage().pixelColor(0, 0) == expected
        assert button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
        icon = button.icon().pixmap(button.iconSize()).toImage()
        themed_icon = window.manager.theme.icon("select-all").pixmap(button.iconSize()).toImage()
        assert not icon.isNull() and icon == themed_icon
        assert filter_button.isVisible() and filter_button.isChecked()
        assert filter_button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
        status_icon = filter_button.icon().pixmap(filter_button.iconSize()).toImage()
        themed_status_icon = window.manager.theme.icon("filter").pixmap(filter_button.iconSize()).toImage()
        assert not status_icon.isNull() and status_icon == themed_status_icon
        return icon, status_icon

    dark_icons = check_sidebar_theme()
    item = _dependency_filter_item(modules, "shared-b")
    modules.setCurrentItem(item)
    modules.setFocus()
    qtbot.keyClick(modules, Qt.Key.Key_Space)
    assert item.checkState() == Qt.CheckState.Checked
    window.manager.set_theme("light")
    assert all(light != dark for light, dark in zip(check_sidebar_theme(), dark_icons, strict=True))
    assert _dependency_filter_list(window) == (panel, modules)
    assert modules.hasFocus() and modules.currentItem() is item
    assert item.checkState() == Qt.CheckState.Checked
    qtbot.keyClick(modules, Qt.Key.Key_Space)
    assert item.checkState() == Qt.CheckState.Unchecked


def test_dependency_filter_header_toggles_all_and_refreshes_shared_views(
    window, legacy_filter_graph_path, qtbot, monkeypatch,
):
    """经实际标题栏批量勾选，每次只提交一次并通知同文件的两个画布。"""
    path = legacy_filter_graph_path
    expected = json.loads(path.read_bytes())
    del expected["modules"][-2]["is_public_dependency"]
    assert window.open_path(path)
    first = window.documents.active
    assert window.manager.duplicate_document(first.id)
    second = window.documents.active
    assert second.document is first.document
    assert window.manager.activate(_DEPENDENCY_FILTER_PANEL_ID)
    button = _dependency_filter_header_button(window)
    _, modules = _dependency_filter_list(window)
    filter_button = _status_filter_button(window)
    assert button.isVisible() and button.isEnabled() and button.focusPolicy() == Qt.FocusPolicy.TabFocus
    assert button.text() == button.toolTip() == "隐藏全部连线"
    assert button.accessibleName() == button.toolTip()
    assert set(_dependency_filter_checks(modules)) == {Qt.CheckState.Checked, Qt.CheckState.Unchecked}
    assert _dependency_arrow_count(first) == _dependency_arrow_count(second) == 14

    commits = []
    changes = []
    commit = QSaveFile.commit

    def count_commit(output):
        commits.append(Path(output.fileName()))
        return commit(output)

    monkeypatch.setattr(QSaveFile, "commit", count_commit)
    first.document.edge_filter_changed.connect(changes.append)
    for count, enabled in enumerate((True, False, True), start=1):
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        assert commits == [path] * count and len(changes) == count
        assert changes[-1] is first.document.content
        state = Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked
        assert _dependency_filter_checks(modules) == [state] * modules.count()
        assert button.text() == ("清除筛选" if enabled else "隐藏全部连线")
        assert button.accessibleName() == button.toolTip() == button.text()
        expected["hidden_edge_module_ids"] = [module["id"] for module in expected["modules"]] if enabled else []
        assert json.loads(path.read_bytes()) == expected
        assert _dependency_arrow_count(first) == _dependency_arrow_count(second) == (0 if enabled else 21)
        assert filter_button.isVisible() == enabled and filter_button.isChecked() == enabled

    assert first.document.set_all_module_edges_hidden(True)
    assert len(commits) == len(changes) == 3
    qtbot.mouseClick(filter_button, Qt.MouseButton.LeftButton)
    assert _dependency_arrow_count(first) == 0 and _dependency_arrow_count(second) == 21
    assert window.manager.close_document(second.id) and window.manager.close_document(first.id)
    assert window.open_path(path)
    assert _dependency_arrow_count(window.documents.active) == 0
    assert _dependency_filter_header_button(window).toolTip() == "清除筛选"
    _, modules = _dependency_filter_list(window)
    assert _dependency_filter_checks(modules) == [Qt.CheckState.Checked] * modules.count()
    assert len(commits) == len(changes) == 3


def test_dependency_filter_header_tracks_active_file_and_individual_checks(window, graph_path, tmp_path, qtbot):
    """切换文件后根据已保存的标记更新按钮，单项勾选和键盘批量操作也能更新。"""
    single_path = tmp_path / "单模块.software-structure"
    single_path.write_text('{"schema_version":1,"modules":[{"id":"core","name":"核心"}]}', "utf-8")
    original = graph_path.read_bytes()
    assert window.open_path(graph_path)
    other = window.documents.active
    assert window.open_path(single_path)
    single = window.documents.active
    assert window.manager.activate(_DEPENDENCY_FILTER_PANEL_ID)
    button = _dependency_filter_header_button(window)
    _, modules = _dependency_filter_list(window)
    assert button.toolTip() == "隐藏全部连线"
    _click_dependency_filter_checkbox(qtbot, modules, _dependency_filter_item(modules, "core"))
    assert button.toolTip() == "清除筛选" and _status_filter_button(window).isHidden()

    window.documents.activate_view(other.id)
    assert button.toolTip() == "隐藏全部连线"
    window.documents.activate_view(single.id)
    assert button.toolTip() == "清除筛选" and button.isEnabled()
    assert window.manager.activate(_DEPENDENCY_FILTER_PANEL_ID)
    button.setFocus()
    qtbot.keyClick(button, Qt.Key.Key_Space)
    assert button.toolTip() == "隐藏全部连线"
    assert _dependency_filter_checks(modules) == [Qt.CheckState.Unchecked]
    assert json.loads(single_path.read_bytes())["hidden_edge_module_ids"] == []
    assert graph_path.read_bytes() == original


def test_dependency_filter_sidebar_edits_active_file_and_restores_marks(window, graph_path, tmp_path, qtbot):
    """切换文件时回显各自的标记，关闭再打开时从磁盘读取，并恢复侧栏显隐。"""
    other_path = tmp_path / "另一份结构.software-structure"
    original = graph_path.read_bytes()
    other_path.write_bytes(original)
    assert window.open_path(graph_path)
    first = window.documents.active
    assert window.open_path(other_path)
    other = window.documents.active
    window.documents.activate_view(first.id)
    button = window.host.right_rail._buttons[_DEPENDENCY_FILTER_PANEL_ID]
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    panel, modules = _dependency_filter_list(window)
    assert panel.isVisible()

    _click_dependency_filter_checkbox(qtbot, modules, _dependency_filter_item(modules, "storage"))
    window.documents.activate_view(other.id)
    saved = json.loads(graph_path.read_bytes())
    assert saved["hidden_edge_module_ids"] == ["storage"]
    assert _dependency_filter_item(modules, "storage").checkState() == Qt.CheckState.Unchecked
    assert other_path.read_bytes() == original
    _click_dependency_filter_checkbox(qtbot, modules, _dependency_filter_item(modules, "ledger"))
    assert window.manager.close_document(other.id)
    assert _dependency_filter_item(modules, "storage").checkState() == Qt.CheckState.Checked
    assert _dependency_filter_item(modules, "ledger").checkState() == Qt.CheckState.Unchecked

    assert window.open_path(other_path)
    reopened = window.documents.active
    assert _dependency_filter_item(modules, "storage").checkState() == Qt.CheckState.Unchecked
    assert _dependency_filter_item(modules, "ledger").checkState() == Qt.CheckState.Checked
    assert _dependency_arrow_count(reopened) == 6
    assert window.new_document(tmp_path / "导图.mindmap")
    assert not panel.isVisible() and not window.manager.is_tool_available(_DEPENDENCY_FILTER_PANEL_ID)
    window.documents.activate_view(reopened.id)
    assert panel.isVisible() and _dependency_filter_item(modules, "ledger").checkState() == Qt.CheckState.Checked
    assert graph_path.read_bytes() != original


@pytest.mark.parametrize(("mindmap_panel_id", "position"), [
    ("outline", DockPosition.LEFT_TOP),
    ("properties", DockPosition.RIGHT_TOP),
])
def test_switching_document_types_preserves_panels_in_shared_slot(
    window, graph_path, tmp_path, qtbot, mindmap_panel_id, position,
):
    """两种标签共用同一停靠格时，切换后恢复各自面板原有的展开状态。"""
    manager = window.manager
    assert window.new_document(tmp_path / "侧栏切换.mindmap")
    mindmap = window.documents.active
    assert manager.activate(mindmap_panel_id, focus=False)
    mindmap_panel = manager.panel(mindmap_panel_id)
    assert mindmap_panel.isVisible()

    assert window.open_path(graph_path)
    graph = window.documents.active
    manager.move(_DEPENDENCY_FILTER_PANEL_ID, DockTarget(position))
    filter_panel = manager.panel(_DEPENDENCY_FILTER_PANEL_ID)
    assert filter_panel.isVisible() and not mindmap_panel.isVisible()

    def click_tab(view):
        group, index = window.host.editor.find_token(view.id)
        tab_bar = group.tabBar()
        qtbot.mouseClick(tab_bar, Qt.MouseButton.LeftButton, pos=tab_bar.tabRect(index).center())
        assert window.documents.active is view

    original_tools = manager.capture_layout()["tools"]
    click_tab(mindmap)
    assert mindmap_panel.isVisible() and not filter_panel.isVisible()
    click_tab(graph)
    assert filter_panel.isVisible() and not mindmap_panel.isVisible()
    assert manager.capture_layout()["tools"] == original_tools

    click_tab(mindmap)
    manager.hide(mindmap_panel_id)
    click_tab(graph)
    assert filter_panel.isVisible()
    click_tab(mindmap)
    assert not mindmap_panel.isVisible() and not filter_panel.isVisible()


@pytest.mark.parametrize("external_edit", [False, True], ids=["short_write", "external_edit"])
@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
def test_dependency_filter_save_failure_restores_marks(
    window, legacy_filter_graph_path, qtbot, monkeypatch, external_edit, bulk,
):
    """短写或提交前发现外部修改时保留文件与已显示的图，后续输入可重新提交。"""
    path = legacy_filter_graph_path
    assert window.open_path(path)
    assert window.manager.activate(_DEPENDENCY_FILTER_PANEL_ID)
    _, modules = _dependency_filter_list(window)
    button = _dependency_filter_header_button(window)
    view = window.documents.active
    filter_button = _status_filter_button(window)
    qtbot.mouseClick(filter_button, Qt.MouseButton.LeftButton)
    assert _dependency_arrow_count(view) == 21 and not filter_button.isChecked()
    scene = view.widget.scene()
    original = path.read_bytes()
    checks = _dependency_filter_checks(modules)
    changes = []
    view.document.edge_filter_changed.connect(changes.append)
    expected = original.replace(b'"/world"', b'"/updated"') if external_edit else original
    write = QSaveFile.write

    def fail_write(output, content):
        if external_edit:
            count = write(output, content)
            path.write_bytes(expected)
            return count
        return write(output, content[:1])

    def change_marks():
        if bulk:
            qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        else:
            _click_dependency_filter_checkbox(qtbot, modules, _dependency_filter_item(modules, "shared-a"))

    existing_files = set(path.parent.iterdir())
    with monkeypatch.context() as patches:
        patches.setattr(QSaveFile, "write", fail_write)
        change_marks()
    message = "文件已在外部修改" if external_edit else "文件内容写入不完整"
    assert message in window.status.currentMessage() and message in window.operation_log.plain_text()
    assert _dependency_filter_checks(modules) == checks and view.widget.scene() is scene
    assert _dependency_arrow_count(view) == 21 and view.widget.shows_all_dependencies
    assert button.toolTip() == "隐藏全部连线" and changes == []
    assert path.read_bytes() == expected and set(path.parent.iterdir()) == existing_files

    if external_edit:
        assert window.manager.close_document(view.id) and window.open_path(path)
        view = window.documents.active
        assert window.manager.activate(_DEPENDENCY_FILTER_PANEL_ID)
    with qtbot.waitSignal(view.document.edge_filter_changed):
        change_marks()
    updated = json.loads(expected)
    del updated["modules"][-2]["is_public_dependency"]
    if bulk:
        updated["hidden_edge_module_ids"] = [module["id"] for module in updated["modules"]]
        assert _dependency_arrow_count(view) == 0 and button.toolTip() == "清除筛选"
    else:
        updated["hidden_edge_module_ids"] = []
        assert _dependency_arrow_count(view) == 21
    assert not view.widget.shows_all_dependencies
    assert json.loads(path.read_bytes()) == updated


def test_empty_graph_has_an_empty_message(window, tmp_path):
    path = tmp_path / "空.software-structure"
    path.write_text('{"schema_version":1,"modules":[]}', "utf-8")
    assert window.open_path(path)
    view = window.documents.active
    assert view.widget.accessibleDescription() == "文件中暂无模块"
    assert [item.text() for item in view.widget.scene().items()] == ["文件中暂无模块"]
    controls, fit, zoom = _status_controls(window)
    assert controls.isVisible()
    assert _status_filter_button(window).isHidden()
    assert not fit.isEnabled() and not zoom.isEnabled()
    assert zoom.text() == "—"
    assert window.manager.activate(_DEPENDENCY_FILTER_PANEL_ID)
    button = _dependency_filter_header_button(window)
    assert button.isVisible() and button.toolTip() == "隐藏全部连线" and not button.isEnabled()


def test_fit_status_icon_matches_mindmap_in_each_theme(window, graph_path, tmp_path):
    """切换活动文档和主题后，两个状态栏按钮使用同一适应窗口图标。"""
    assert window.new_document(tmp_path / "图标对照.mindmap")
    mindmap = window.documents.active
    mindmap_controls = window.status.findChild(QWidget, "mindmap_status_controls")
    assert mindmap_controls is not None
    mindmap_fit = mindmap_controls.findChild(QToolButton, "status_fit")
    assert mindmap_fit is not None
    assert window.open_path(graph_path)
    graph = window.documents.active
    _, fit, _ = _status_controls(window)

    for theme in ("light", "dark"):
        window.documents.activate_view(mindmap.id)
        window.manager.set_theme(theme)
        assert mindmap_fit.isVisible()
        mindmap_icon = mindmap_fit.icon()
        window.documents.activate_view(graph.id)
        assert fit.isVisible() and fit.iconSize() == mindmap_fit.iconSize()
        shared_icon = window.manager.theme.icon("fit-view")
        assert not shared_icon.isNull()
        for mode in (QIcon.Mode.Normal, QIcon.Mode.Disabled):
            expected = shared_icon.pixmap(fit.iconSize(), mode).toImage()
            assert mindmap_icon.pixmap(fit.iconSize(), mode).toImage() == expected
            assert fit.icon().pixmap(fit.iconSize(), mode).toImage() == expected


@pytest.mark.parametrize("interruption", ["focus", "capture"])
def test_canvas_drag_from_a_module_ends_on_interruption(window, graph_path, qtbot, interruption):
    """右键超过阈值才平移，左键拖过阈值即取消点击，捕获中断后复位光标。"""
    assert window.open_path(graph_path)
    view = window.documents.active
    canvas = view.widget
    qtbot.waitUntil(lambda: "zoom" in view.capture_state())
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


def test_viewport_interactions_duplicate_and_restart(window, graph_path, qtbot, tmp_path):
    graph = json.loads(graph_path.read_bytes())
    graph["hidden_edge_module_ids"] = ["cache"]
    graph_path.write_text(json.dumps(graph, ensure_ascii=False), "utf-8")
    original_bytes = graph_path.read_bytes()
    assert window.open_path(graph_path)
    first = window.documents.active
    canvas = first.widget
    qtbot.waitUntil(lambda: bool(first.capture_state()))
    fitted = first.capture_state()
    _, fit, zoom = _status_controls(window)
    filter_button = _status_filter_button(window)
    assert filter_button.isChecked() and _dependency_arrow_count(first) == 8
    assert fit.isEnabled() and zoom.isEnabled()
    assert zoom.text() == f"{fitted['zoom']:.0%}"
    _click_module_title(qtbot, canvas, "订单服务")

    viewport = canvas.viewport()
    anchor = viewport.rect().center() + QPoint(28, 18)
    scene_anchor = canvas.mapToScene(anchor)
    event = QWheelEvent(QPointF(anchor), QPointF(viewport.mapToGlobal(anchor)), QPoint(), QPoint(0, 120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False)
    QApplication.sendEvent(viewport, event)
    zoomed = first.capture_state()
    assert zoomed["zoom"] > fitted["zoom"]
    assert zoom.text() == f"{zoomed['zoom']:.0%}"
    assert QLineF(scene_anchor, canvas.mapToScene(anchor)).length() < 2

    start = QPoint(48, 64)
    end = start + QPoint(62, 37)
    assert canvas.itemAt(start) is None
    qtbot.mousePress(viewport, Qt.MouseButton.RightButton, pos=start)
    qtbot.mouseMove(viewport, end)
    drag_cursor = viewport.cursor().shape()
    qtbot.mouseRelease(viewport, Qt.MouseButton.RightButton, pos=end)
    assert drag_cursor == Qt.CursorShape.ClosedHandCursor
    assert viewport.cursor().shape() != Qt.CursorShape.ClosedHandCursor
    panned = first.capture_state()
    assert panned["center"][0] < zoomed["center"][0] - 20
    assert panned["center"][1] < zoomed["center"][1] - 10
    assert _highlighted_dependencies(canvas) == {"app → orders", "orders → billing", "orders → storage"}

    qtbot.mouseClick(filter_button, Qt.MouseButton.LeftButton)
    assert _dependency_arrow_count(first) == 9 and first.capture_state() == panned
    assert window.manager.duplicate_document(first.id)
    copied = window.documents.active
    assert copied.document is first.document and copied.widget is not canvas
    assert filter_button.isChecked() and _dependency_arrow_count(copied) == 8
    assert _highlighted_dependencies(copied.widget) == set()
    qtbot.waitUntil(lambda: copied.widget.transform().m11() == pytest.approx(panned["zoom"]))
    assert copied.capture_state()["center"] == pytest.approx(panned["center"], abs=2)
    assert zoom.text() == f"{panned['zoom']:.0%}"

    center = _visible_center(copied.widget)
    qtbot.mouseClick(zoom, Qt.MouseButton.LeftButton)
    assert copied.capture_state()["zoom"] == 1.0 and zoom.text() == "100%"
    assert QLineF(_visible_center(copied.widget), center).length() < 2

    qtbot.mouseClick(fit, Qt.MouseButton.LeftButton)
    assert copied.capture_state()["zoom"] == pytest.approx(fitted["zoom"])
    assert copied.capture_state()["center"] == pytest.approx(fitted["center"], abs=2)
    assert zoom.text() == f"{fitted['zoom']:.0%}"
    assert first.capture_state()["zoom"] == panned["zoom"]
    assert first.capture_state()["center"] == pytest.approx(panned["center"], abs=2)
    _click_module_title(qtbot, copied.widget, "商品服务")
    assert _highlighted_dependencies(copied.widget) == {"app → catalog", "catalog → storage"}
    qtbot.mouseClick(filter_button, Qt.MouseButton.LeftButton)
    assert _dependency_arrow_count(copied) == 9 and copied.widget.shows_all_dependencies
    states = {item.id: item.capture_state() for item in (first, copied)}
    assert window.state.recent_files == [str(graph_path)]
    assert window.close()

    restored = build_window(storage_dir=tmp_path / "配置")
    qtbot.addWidget(restored, before_close_func=lambda item: item.dispose())
    restored.show()
    assert restored.documents.active.id == copied.id
    assert restored.documents.views[first.id].document is restored.documents.views[copied.id].document
    assert restored.state.recent_files == [str(graph_path)]
    controls, restored_fit, restored_zoom = _status_controls(restored)
    restored_filter = _status_filter_button(restored)
    assert controls.isVisible()
    for view_id, state in states.items():
        restored.documents.activate_view(view_id)
        view = restored.documents.active
        qtbot.waitUntil(lambda: view.widget.transform().m11() == pytest.approx(state["zoom"]))
        assert view.capture_state()["center"] == pytest.approx(state["center"], abs=2)
        assert restored_zoom.text() == f"{state['zoom']:.0%}"
        assert _highlighted_dependencies(view.widget) == set()
        assert restored_filter.isChecked() and _dependency_arrow_count(view) == 8
    # 旧会话的全量或展开状态由当前文件筛选替代，边界内的视口仍可恢复。
    restored.documents.activate_view(first.id)
    view = restored.documents.active
    view.restore_state({"version": 5, "zoom": 1.2, "center": fitted["center"],
                        "show_all_dependencies": True, "expanded_public_module": "cache"})
    assert restored_filter.isChecked() and _dependency_arrow_count(view) == 8
    assert view.capture_state()["zoom"] == pytest.approx(1.2)
    assert view.capture_state()["center"] == pytest.approx(fitted["center"], abs=2)
    # 重启后视口尺寸可能变化。无效的旧视口应回到当前窗口的全图视图。
    view.restore_state({"version": 1, "zoom": float("nan"), "center": [0, 0]})
    fallback = view.capture_state()
    qtbot.mouseClick(restored_fit, Qt.MouseButton.LeftButton)
    fitted_after_restart = view.capture_state()
    assert fallback["zoom"] == pytest.approx(fitted_after_restart["zoom"])
    assert fallback["center"] == pytest.approx(fitted_after_restart["center"], abs=2)
    assert restored.close()
    assert graph_path.read_bytes() == original_bytes


def test_status_controls_follow_focused_split(window, graph_path, qtbot):
    """两个分屏同时可见时，状态栏跟随键盘焦点，后台画布的倍率变化不会覆盖读数。"""
    assert window.open_path(graph_path)
    first = window.documents.active
    assert window.manager.duplicate_document(first.id)
    second = window.documents.active
    window.host.editor.split_token(first.id, Qt.Orientation.Horizontal)
    qtbot.waitUntil(lambda: first.widget.isVisible() and second.widget.isVisible())
    _, fit, zoom = _status_controls(window)
    window.activateWindow()
    first.widget.setFocus()
    qtbot.waitUntil(lambda: window.documents.active is first and first.widget.hasFocus())

    first.restore_state({"version": 1, "zoom": 1.4, "center": [100, 50]})
    second.restore_state({"version": 1, "zoom": 2.0, "center": [200, 70]})
    assert zoom.text() == "140%"
    qtbot.mouseClick(zoom, Qt.MouseButton.LeftButton)
    assert first.widget.zoom_factor == 1.0 and second.widget.zoom_factor == 2.0
    assert first.widget.hasFocus()

    second.widget.setFocus()
    qtbot.waitUntil(lambda: window.documents.active is second)
    assert zoom.text() == "200%"
    qtbot.mouseClick(fit, Qt.MouseButton.LeftButton)
    assert second.widget.zoom_factor <= 1.0 and first.widget.zoom_factor == 1.0
    assert zoom.text() == f"{second.widget.zoom_factor:.0%}"


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
    assert window.open_path(path)
    view = window.documents.active
    qtbot.waitUntil(lambda: bool(view.capture_state()))
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
    controls, _, _ = _status_controls(window)
    mindmap_controls = window.status.findChild(QWidget, "mindmap_status_controls")
    assert controls.isVisible() and not mindmap_controls.isVisible()
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
    assert mindmap_controls.isVisible() and not controls.isVisible()
    assert mindmap.document.path == target
    assert window.action("app.new").isEnabled()
    open_file_menu()
    assert window.action("app.new") in files.actions() and window.action("app.new").text() == "新建导图"
    assert any(action.text() == "导出当前页" for action in files.actions())
    files.close()
    mindmap.engine.update_node(mindmap.engine.root_id, text="批量保存的内容")
    mindmap.document.timer.stop()
    window.documents.activate_view(graph.id)
    assert controls.isVisible() and not mindmap_controls.isVisible()
    assert not window.action("app.save").isEnabled()
    assert window.action("app.save_all").isEnabled()
    assert window.save_all()
    assert load_document(target).root.text == "批量保存的内容"
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(target), ""))
    window.action("app.open").trigger()
    assert window.documents.active is mindmap
    assert window.manager.close_document(graph.id)
