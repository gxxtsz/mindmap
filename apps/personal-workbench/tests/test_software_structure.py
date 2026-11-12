"""通过产品入口验收软件结构文档的浏览、恢复和只读行为。"""

import json
from pathlib import Path

import pytest
from mindmap_core.formats import load_document
from personal_workbench.app import build_window
from PySide6.QtCore import QLineF, QPoint, QPointF, QSaveFile, QStandardPaths, Qt, QTimer
from PySide6.QtGui import QIcon, QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsSimpleTextItem,
    QMessageBox,
    QToolButton,
    QWidget,
)
from workbench_api import SavePolicy

_DOWNLOAD_ACTION_ID = "software-structure.download-template"


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


def _status_controls(window):
    """在窗口状态栏中定位软件结构插件的实际按钮。"""
    controls = window.status.findChild(QWidget, "software_structure_status_controls")
    assert controls is not None
    fit = controls.findChild(QToolButton, "status_fit")
    zoom = controls.findChild(QToolButton, "status_reset_zoom")
    assert fit is not None and zoom is not None
    return controls, fit, zoom


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


def test_empty_graph_has_an_empty_message(window, tmp_path):
    path = tmp_path / "空.software-structure"
    path.write_text('{"schema_version":1,"modules":[]}', "utf-8")
    assert window.open_path(path)
    view = window.documents.active
    assert view.widget.accessibleDescription() == "文件中暂无模块"
    assert [item.text() for item in view.widget.scene().items()] == ["文件中暂无模块"]
    controls, fit, zoom = _status_controls(window)
    assert controls.isVisible()
    assert not fit.isEnabled() and not zoom.isEnabled()
    assert zoom.text() == "—"


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


def test_viewport_interactions_duplicate_and_restart(window, graph_path, qtbot, tmp_path):
    original_bytes = graph_path.read_bytes()
    assert window.open_path(graph_path)
    first = window.documents.active
    canvas = first.widget
    qtbot.waitUntil(lambda: bool(first.capture_state()))
    fitted = first.capture_state()
    _, fit, zoom = _status_controls(window)
    assert fit.isEnabled() and zoom.isEnabled()
    assert zoom.text() == f"{fitted['zoom']:.0%}"

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
    states = {item.id: item.capture_state() for item in (first, copied)}
    assert window.state.recent_files == [str(graph_path)]
    assert window.close()

    restored = build_window(storage_dir=tmp_path / "配置")
    qtbot.addWidget(restored, before_close_func=lambda item: item.dispose())
    restored.show()
    assert restored.documents.active.id == copied.id
    assert restored.documents.views[first.id].document is restored.documents.views[copied.id].document
    assert restored.state.recent_files == [str(graph_path)]
    controls, _, restored_zoom = _status_controls(restored)
    assert controls.isVisible()
    for view_id, state in states.items():
        restored.documents.activate_view(view_id)
        view = restored.documents.active
        qtbot.waitUntil(lambda: view.widget.transform().m11() == pytest.approx(state["zoom"]))
        assert view.capture_state()["center"] == pytest.approx(state["center"], abs=2)
        assert restored_zoom.text() == f"{state['zoom']:.0%}"
    view.restore_state({"version": 1, "zoom": float("nan"), "center": [0, 0]})
    assert view.capture_state()["zoom"] == pytest.approx(fitted["zoom"])
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
