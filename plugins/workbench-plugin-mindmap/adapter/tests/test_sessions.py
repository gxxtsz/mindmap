"""多导图会话：独立状态、自动保存、原子关闭、文件归属及外壳布局兼容。"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from importlib.util import find_spec
from threading import Event, Timer, get_ident

import pytest
from mindmap_core import LayoutConfig, MindMapDocument, MindMapError, Node, Theme
from mindmap_formats import load_document, save_document
from mindmap_qt_canvas import loading as background_loading
from personal_workbench.app import build_window
from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPoint, QPointF, QRectF, QSize, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent, QImage, QInputMethodEvent
from PySide6.QtWidgets import QApplication, QFileDialog, QPushButton, QToolButton, QWidget
from shiboken6 import isValid


@pytest.fixture
def empty_window(qtbot, tmp_path):
    """每例使用独立配置和文件目录；失败注入不影响测试结束时的资源回收。"""
    window = build_window(storage_dir=tmp_path / "配置", restore=False)

    def clean(item):
        for session in item.plugins.instance("mindmap").sessions.values():
            session.panels.properties.discard_pending()
            session.canvas.finish_edit(commit=False)
            session.engine.mark_saved()

    qtbot.addWidget(window, before_close_func=clean)
    window.show()
    QApplication.processEvents()
    yield window
    clean(window)
    window.close()


def create(window, path):
    """显式路径的新建不需要对话框，返回具有独立组件的已保存会话。"""
    assert window.new_document(path)
    return window.plugins.instance("mindmap").active_session


@pytest.fixture
def image_clipboard(qapp):
    """图片集成测试借用系统剪贴板；原生平台结束后恢复，离屏平台提前释放载荷。"""
    clipboard = qapp.clipboard()
    original = QMimeData()
    previous = clipboard.mimeData()
    if previous is not None:
        for kind in previous.formats():
            original.setData(kind, previous.data(kind))
        if previous.hasImage():
            original.setImageData(previous.imageData())
    yield clipboard
    if qapp.platformName() == "offscreen":
        clipboard.clear()
    else:
        clipboard.setMimeData(original)


def create_large_session(window, path):
    """构造超过后台保存阈值的导图，初始造数不计入任何交互或后台任务。"""
    session = create(window, path)
    document = MindMapDocument.new("大图保存")
    keys = [document.root_id]
    for index in range(1100):
        node = Node(f"节点 {index}")
        document.nodes[keys[index // 5]].children.append(node.id)
        document.nodes[node.id] = node
        keys.append(node.id)
    session.engine.replace_document(document)
    return session


@pytest.fixture
def controlled_save_executor(monkeypatch):
    """用可控执行器注入竞态，实际进程路径另由完整工作台基准及独立进程验收覆盖。"""
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr("workbench_plugin_mindmap.documents._SAVE_POOL", executor)
        yield executor


def test_real_background_process_saves_without_running_in_gui_process(
    empty_window, tmp_path, qtbot, monkeypatch,
):
    """真实 spawn/pythonw 入口验收，覆盖 Windows venv 重定向及进程间模型传输。"""
    import workbench_plugin_mindmap.documents as sessions_module

    # 模拟工作台从包入口启动。spawn 会跳过包的 __main__ 模块，避免在无标准输出的
    # pythonw 进程中重载 PyCharm 测试启动器；实际进程创建、快照传输及写盘照常执行。
    monkeypatch.setattr(sys.modules["__main__"], "__spec__", find_spec("personal_workbench.__main__"))
    session = create_large_session(empty_window, tmp_path / "真实后台进程.mindmap")
    try:
        session.engine.update_node(session.engine.root_id, text="进程保存成功")
        session.document.timer.stop()
        assert session.document.autosave(background=True)
        future = session.document._background_save[0]
        qtbot.waitUntil(lambda: session.document._background_save is None, timeout=15000)
        assert future.result() != os.getpid()
        assert not session.document.last_error and not session.engine.is_dirty
        assert load_document(session.files.path) == session.engine.document
    finally:
        session.document._finish_background_save(wait=True)
        if sessions_module._SAVE_POOL is not None:
            sessions_module._SAVE_POOL.shutdown(wait=True, cancel_futures=True)
            sessions_module._SAVE_POOL = None


def test_background_save_uses_stable_version_without_marking_later_edit_saved(
    empty_window, tmp_path, monkeypatch, qtbot, controlled_save_executor
):
    session = create_large_session(empty_window, tmp_path / "后台版本.mindmap")
    started, release = Event(), Event()
    ui_thread = get_ident()
    worker_threads = []

    def held_writer(document, path, **options):
        worker_threads.append(get_ident())
        started.set()
        assert release.wait(5)
        save_document(document, path, **options)

    monkeypatch.setattr("workbench_plugin_mindmap.documents.save_document", held_writer)
    monkeypatch.setattr("workbench_plugin_mindmap.documents.save_snapshot", held_writer)
    engine = session.engine
    try:
        engine.update_node(engine.root_id, text="捕获版本甲")
        session.document.timer.stop()
        assert session.document.autosave(background=True)
        qtbot.waitUntil(started.is_set)
        assert worker_threads == [worker_threads[0]] and worker_threads[0] != ui_thread
        engine.update_node(engine.root_id, text="后台期间的版本乙")
        session.document.timer.stop()
        release.set()
        qtbot.waitUntil(lambda: session.document._background_save is None)
        session.document.timer.stop()
        assert load_document(session.files.path).root.text == "捕获版本甲"
        assert engine.is_dirty
        assert engine.undo() and not engine.is_dirty
        assert engine.redo() and engine.is_dirty
        monkeypatch.setattr("workbench_plugin_mindmap.documents.save_document", save_document)
        monkeypatch.setattr("workbench_plugin_mindmap.documents.save_snapshot", save_document)
        assert session.document.autosave() and not engine.is_dirty
        assert load_document(session.files.path).root.text == "后台期间的版本乙"
    finally:
        release.set()
        session.document._finish_background_save(wait=True)


@pytest.mark.parametrize("changed", ["registry", "codec"])
def test_background_request_preserves_custom_format_dispatch(empty_window, tmp_path, monkeypatch, changed):
    """覆写注册表或编码器时使用原同步入口，不能在后台重建默认编码器绕过扩展。"""
    session = create_large_session(empty_window, tmp_path / "扩展同步保存.mindmap")
    registry = session.canvas.format_registry
    provider = registry if changed == "registry" else registry.get("mindmap").exporter
    original, calls = provider.write, []

    def track(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(provider, "write", track)
    monkeypatch.setattr("workbench_plugin_mindmap.documents._save_pool", lambda: pytest.fail("扩展保存不能建立后台进程"))
    session.engine.update_node(session.engine.root_id, text="保留自定义保存入口")
    session.document.timer.stop()
    assert session.document.autosave(background=True)
    assert calls and session.document._background_save is None and not session.engine.is_dirty
    assert load_document(session.files.path).root.text == "保留自定义保存入口"


def test_background_failure_keeps_old_file_and_explicit_retry_succeeds(
    empty_window, tmp_path, monkeypatch, qtbot, controlled_save_executor
):
    session = create_large_session(empty_window, tmp_path / "后台失败.mindmap")
    original = session.files.path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("后台磁盘写入失败")

    monkeypatch.setattr("workbench_plugin_mindmap.documents.save_document", fail)
    monkeypatch.setattr("workbench_plugin_mindmap.documents.save_snapshot", fail)
    session.engine.update_node(session.engine.root_id, text="必须保留的修改")
    session.document.timer.stop()
    assert session.document.autosave(background=True)
    qtbot.waitUntil(lambda: session.document._background_save is None)
    assert "后台磁盘" in session.document.last_error and session.engine.is_dirty
    assert session.files.path.read_bytes() == original and not session.document.timer.isActive()
    monkeypatch.setattr("workbench_plugin_mindmap.documents.save_document", save_document)
    monkeypatch.setattr("workbench_plugin_mindmap.documents.save_snapshot", save_document)
    assert session.document.save() and not session.engine.is_dirty
    assert load_document(session.files.path).root.text == "必须保留的修改"


def test_explicit_save_waits_for_old_background_write_before_writing_new_version(
    empty_window, tmp_path, monkeypatch, qtbot, controlled_save_executor
):
    session = create_large_session(empty_window, tmp_path / "保存次序.mindmap")
    started, release = Event(), Event()
    ui_thread, writes = get_ident(), []

    def held_writer(document, path, **options):
        writes.append(document.root.text)
        if get_ident() != ui_thread:
            started.set()
            assert release.wait(5)
        save_document(document, path, **options)

    monkeypatch.setattr("workbench_plugin_mindmap.documents.save_document", held_writer)
    monkeypatch.setattr("workbench_plugin_mindmap.documents.save_snapshot", held_writer)
    try:
        session.engine.update_node(session.engine.root_id, text="先保存的版本")
        session.document.timer.stop()
        assert session.document.autosave(background=True)
        qtbot.waitUntil(started.is_set)
        session.engine.update_node(session.engine.root_id, text="最终版本")
        Timer(0.03, release.set).start()
        assert session.files.save()
        assert session.document._background_save is None and not session.engine.is_dirty
        assert writes == ["先保存的版本", "最终版本"]
        assert load_document(session.files.path).root.text == "最终版本"
    finally:
        release.set()
        session.document._finish_background_save(wait=True)


def assert_initial_canvas(canvas):
    """用实际视口映射检查 100% 与根节点居中，不用整个场景的中心替代根节点。"""
    assert canvas.view.transform().isIdentity()
    root = canvas.node_items[canvas.engine.root_id].sceneBoundingRect().center()
    offset = canvas.view.viewportTransform().map(root) - QRectF(canvas.view.viewport().rect()).center()
    assert abs(offset.x()) <= 2 and abs(offset.y()) <= 2


@pytest.mark.parametrize("before_show", [False, True])
def test_open_initializes_after_session_and_sidebar_layout(empty_window, tmp_path, qtbot, before_show):
    """普通打开和命令行的先加载后显示顺序，都在真实侧栏布局下恢复 100% 并居中。"""
    window = empty_window
    if before_show:
        window.hide()
    document = MindMapDocument.new("大型导图根节点")
    document.layout = LayoutConfig(direction="down")
    for index in range(100):
        child = Node(f"向下展开的分支 {index}")
        document.nodes[child.id] = child
        document.root.children.append(child.id)
    source = tmp_path / "大导图.mindmap"
    save_document(document, source)
    original = source.read_bytes()
    assert window.open_path(source)
    canvas = window.plugins.instance("mindmap").canvas
    window.manager.activate("outline", focus=False)
    window.resize(1500, 900)
    window.show()
    qtbot.waitUntil(lambda: assert_initial_canvas(canvas))
    assert canvas.engine.document == document
    assert not canvas.engine.is_dirty and not canvas.engine.can_undo
    assert source.read_bytes() == original


def test_rapid_open_initializes_hidden_tab_once_and_duplicate_open_keeps_view(empty_window, tmp_path, qtbot):
    """快速打开后被遮挡的标签延迟初始化；已打开文件再次激活时保留用户视图。"""
    window = empty_window
    sources = [tmp_path / f"标签 {index}.mindmap" for index in range(2)]
    for index, source in enumerate(sources):
        save_document(MindMapDocument.new(f"根主题 {index}"), source)
    assert window.open_path(sources[0])
    first = window.plugins.instance("mindmap").active_session
    first.canvas.view.scale(0.4, 0.4)
    assert window.open_path(sources[1])
    second = window.plugins.instance("mindmap").active_session
    qtbot.waitUntil(lambda: assert_initial_canvas(second.canvas))
    assert first.canvas.view.transform().m11() == pytest.approx(0.4)
    window.manager.activate_document(first.id)
    qtbot.waitUntil(lambda: assert_initial_canvas(first.canvas))
    view = first.canvas.view
    view.zoom(0.7)
    root = first.canvas.node_items[first.engine.root_id].sceneBoundingRect().center()
    view.centerOn(root + QPointF(90, -60))
    center = view.mapToScene(view.viewport().rect()).boundingRect().center()
    transform = view.transform()
    window.manager.activate_document(second.id)
    assert window.open_path(sources[0])
    QApplication.processEvents()
    assert window.plugins.instance("mindmap").active_session is first and len(window.plugins.instance("mindmap").sessions) == 2
    assert view.transform() == transform
    assert view.mapToScene(view.viewport().rect()).boundingRect().center() == center
    # 改变侧栏和窗口后继续以原用户中心导航，不再次消费首次打开的定位请求。
    window.manager.activate("outline", focus=False)
    window.resize(1400, 950)
    QApplication.processEvents()
    assert view.transform() == transform
    actual = view.mapToScene(view.viewport().rect()).boundingRect().center()
    assert (actual - center).manhattanLength() < 2 / transform.m11()


@pytest.mark.parametrize("operation", ["new", "open"])
def test_close_before_initialization_releases_canvas(empty_window, tmp_path, operation):
    """会话尚有排队初始化就关闭时，不访问已经删除的 Qt 对象。"""
    source = tmp_path / "立即关闭.mindmap"
    if operation == "open":
        save_document(MindMapDocument.new("立即关闭的根"), source)
        assert empty_window.open_path(source)
    else:
        assert empty_window.new_document(source)
    session = empty_window.plugins.instance("mindmap").active_session
    canvas = session.canvas
    assert empty_window.manager.close_document(session.id)
    assert session.disposed
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()
    assert not isValid(canvas)


def test_outline_header_stays_icon_only_after_empty_start_and_session_switch(empty_window, tmp_path):
    """空白页先创建大纲标题，再打开和切换导图时，延迟绑定的动作仍只显示图标。"""
    window = empty_window
    button, = window.manager.panel("outline").action_buttons

    def check(session):
        """验证实际按钮的显示、菜单交互和层级提示均绑定当前会话。"""
        assert button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
        assert not button.icon().isNull() and button.size() == QSize(28, 28)
        assert button.popupMode() == QToolButton.ToolButtonPopupMode.InstantPopup
        assert button.defaultAction().menu() is session.panels.outline.depth_menu
        assert button.toolTip() == session.panels.outline.depth_action.toolTip()

    first = create(window, tmp_path / "第一份.mindmap")
    window.manager.activate("outline", focus=False)
    check(first)
    first.panels.outline.set_display_depth(4)
    check(first)
    second = create(window, tmp_path / "第二份.mindmap")
    check(second)
    window.manager.activate_document(first.id)
    check(first)
    assert window.manager.close_document(first.id)
    assert window.manager.close_document(second.id)
    assert not button.isEnabled()
    third = create(window, tmp_path / "重新打开.mindmap")
    check(third)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_welcome_actions_and_last_close(empty_window, tmp_path, theme):
    window = empty_window
    window.manager.set_theme(theme)
    assert len(window.plugins.instance("mindmap").sessions) == 0 and window.plugins.instance("mindmap").active_session is None
    assert window.plugins.instance("mindmap").canvas is window.plugins.instance("mindmap").engine is window.plugins.instance("mindmap").files is window.plugins.instance("mindmap").panels is None
    assert window.manager.documents == {}
    group = window.host.editor.groups[0]
    assert group.empty_widget.isVisible() and group.count() == 0
    assert {button.text() for button in group.empty_widget.findChildren(QPushButton)} == {"新建导图", "新建文本", "打开文件", "插件管理"}
    assert not any(window.action("app." + key).isEnabled()
                   for key in ("open_directory", "save", "save_all", "save_as"))
    assert not window.plugins.instance("mindmap").settings_action.isEnabled()
    assert window.host.left.isHidden() and window.host.right.isHidden()
    assert not window.plugins.instance("mindmap")._depth_action.isVisible()
    for tool_id in ("outline", "properties"):
        assert not window.manager.is_tool_available(tool_id)
        assert not window.manager.activate(tool_id)
        assert not window.manager.actions["tool:" + tool_id].isEnabled()
    menu = window.main_menu()
    files = menu.actions()[0].menu()
    assert [action.text() for action in files.actions()] == [
        "新建", "新建导图", "新建文本", "打开…", "打开所在文件夹", "保存", "保存全部", "另存为…", "退出",
    ]
    for title in ("导出图形", "导出当前页为 TXT…"):
        assert all(action.text() != title for action in files.actions())
    assert not menu.actions()[1].menu().actions()[0].isEnabled()
    menu.deleteLater()
    session = create(window, tmp_path / "第一份.mindmap")
    assert not group.empty_widget.isVisible() and group.count() == 1
    assert window.host.right.isVisible() and session.panels.properties.isVisible()
    session.engine.update_node(session.engine.root_id, text="关闭时立即保存")
    assert window.manager.close_document(session.id)
    assert load_document(session.files.path).root.text == "关闭时立即保存"
    assert window.plugins.instance("mindmap").active_session is None and not window.manager.documents
    assert group.empty_widget.isVisible() and session.disposed and session.canvas._disposed
    assert window.host.left.isHidden() and window.host.right.isHidden()
    assert not window.plugins.instance("mindmap")._depth_action.isVisible()


def test_closing_split_sessions_keeps_one_usable_welcome_page(empty_window, tmp_path, qtbot, monkeypatch):
    """真实工作台收起空分屏时完成自动保存和释放，最后的欢迎页仍可直接新建导图。"""
    window, area = empty_window, empty_window.host.editor
    first = create(window, tmp_path / "左侧.mindmap")
    second = create(window, tmp_path / "右侧.mindmap")
    area.split_token(second.id, Qt.Orientation.Horizontal)
    third = create(window, tmp_path / "下侧.mindmap")
    area.split_token(third.id, Qt.Orientation.Vertical)
    last_group = area.find_token(third.id)[0]
    for session in (first, second, third):
        session.engine.update_node(session.engine.root_id, text="关闭前保存：" + session.files.path.stem)

    for session, remaining in ((second, 2), (first, 1), (third, 1)):
        assert window.manager.close_document(session.id)
        assert len(area.groups) == remaining
        assert session.disposed
        assert load_document(session.files.path).root.text == "关闭前保存：" + session.files.path.stem
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()
    assert area.groups == [last_group] and last_group.empty_widget.isVisible()
    assert last_group.count() == 0 and window.plugins.instance("mindmap").active_session is None
    assert not window.manager.documents and not len(window.plugins.instance("mindmap").sessions)
    assert window.host.left.isHidden() and window.host.right.isHidden()
    assert all(not isValid(session.canvas) for session in (first, second, third))

    target = tmp_path / "欢迎页重新新建.mindmap"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(target), ""))
    button = next(button for button in last_group.empty_widget.findChildren(QPushButton)
                  if button.text() == "新建导图")
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert area.groups == [last_group] and last_group.count() == 1
    assert window.plugins.instance("mindmap").active_session.files.path == target
    assert last_group.currentWidget() is window.plugins.instance("mindmap").active_session.canvas
    assert not last_group.empty_widget.isVisible()


def test_closing_neighbor_split_preserves_inline_draft(empty_window, tmp_path, qtbot):
    """合并相邻空分屏不结束当前导图的原地文字编辑，也不提前提交草稿。"""
    window = empty_window
    first = create(window, tmp_path / "继续编辑.mindmap")
    second = create(window, tmp_path / "关闭相邻.mindmap")
    window.host.editor.split_token(second.id, Qt.Orientation.Horizontal)
    window.manager.activate_document(first.id)
    first.canvas.begin_edit(first.engine.root_id)
    editor = first.canvas.editor
    editor.setPlainText("尚未提交的草稿")
    qtbot.waitUntil(first.canvas.view.hasFocus)
    assert window.manager.close_document(second.id)
    QApplication.processEvents()
    assert first.canvas.view.hasFocus() and first.canvas.editor is editor
    assert editor.toPlainText() == "尚未提交的草稿"
    assert first.engine.document.root.text == "中心主题"
    assert window.plugins.instance("mindmap").active_session is first and len(window.host.editor.groups) == 1
    # 临时保护应在合并结束时解除，随后正常切换到工具仍须按失焦规则提交草稿。
    window.manager.activate("outline")
    qtbot.waitUntil(lambda: first.canvas.editor is None)
    assert first.engine.document.root.text == "尚未提交的草稿"


def test_empty_start_preserves_sidebar_layout_across_restart(empty_window, tmp_path, qtbot):
    """有导图时保存的侧栏布局在空启动时暂停，重新打开导图后恢复原位置与宽度。"""
    window = empty_window
    first = create(window, tmp_path / "布局.mindmap")
    window.manager.activate("outline", focus=False)
    window.host.region_sizes["dock"].update(left=310, right=390)
    window.manager.refresh()
    original = window.manager.capture_layout()
    assert window.manager.close_document(first.id)
    assert window.host.left.isHidden() and window.host.right.isHidden()
    assert window.manager.capture_layout()["tools"] == original["tools"]
    window.manager.save_session()
    assert window.close()

    restarted = build_window(storage_dir=window.manager.storage_dir)
    qtbot.addWidget(restarted)
    try:
        restarted.show()
        QApplication.processEvents()
        assert restarted.host.left.isHidden() and restarted.host.right.isHidden()
        assert restarted.host.editor.groups[0].empty_widget.isVisible()
        # 切换主题和恢复布局也不能在没有导图时显示空侧栏。
        restarted.manager.set_theme("dark")
        restarted.manager.restore_layout(original)
        assert restarted.host.left.isHidden() and restarted.host.right.isHidden()
        assert restarted.manager.capture_layout()["tools"] == original["tools"]
        assert restarted.open_path(first.files.path)
        assert restarted.host.left.isVisible() and restarted.host.right.isVisible()
        assert restarted.host.region_sizes["dock"] == original["hosts"]["main"]["region_sizes"]["dock"]
        assert restarted.plugins.instance("mindmap").panels.outline.isVisible() and restarted.plugins.instance("mindmap").panels.properties.isVisible()
    finally:
        restarted.close()


def test_new_cancel_failure_and_path_collision_leave_sessions_unchanged(empty_window, tmp_path, monkeypatch):
    window = empty_window
    first = create(window, tmp_path / "已有.mindmap")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: ("", ""))
    assert not window.new_document()
    assert not window.new_document(first.files.path)
    assert not window.new_document(tmp_path / "不存在的目录" / "失败.mindmap")
    assert list(window.plugins.instance("mindmap").sessions.values()) == [first] and window.plugins.instance("mindmap").active_session is first
    assert load_document(first.files.path).root.text == "中心主题"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(tmp_path / "第二份"), ""))
    assert window.new_document() and len(window.plugins.instance("mindmap").sessions) == 2
    assert window.plugins.instance("mindmap").files.path == tmp_path / "第二份.mindmap"


def test_duplicate_open_preserves_split_position_memory_and_path_case(empty_window, tmp_path):
    window = empty_window
    first = create(window, tmp_path / "One.mindmap")
    second = create(window, tmp_path / "Two.mindmap")
    window.host.editor.split_token(first.id, Qt.Orientation.Horizontal)
    first.engine.update_node(first.engine.root_id, text="不从磁盘重载")
    first.document.timer.stop()
    location = window.host.editor.find_token(first.id)
    window.manager.activate_document(second.id)
    assert window.open_path(str(first.files.path).upper())
    assert window.plugins.instance("mindmap").active_session is first and len(window.plugins.instance("mindmap").sessions) == 2
    assert window.host.editor.find_token(first.id) == location
    assert first.engine.document.root.text == "不从磁盘重载"
    assert first.engine.can_undo


def test_save_as_cannot_overwrite_other_session(empty_window, tmp_path, monkeypatch):
    window = empty_window
    first = create(window, tmp_path / "甲.mindmap")
    second = create(window, tmp_path / "乙.mindmap")
    second.engine.update_node(second.engine.root_id, text="第二份修改")
    original = first.files.path.read_bytes()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(first.files.path), ""))
    assert not window.save(save_as=True)
    assert second.files.path == tmp_path / "乙.mindmap" and second.files.is_dirty
    assert first.files.path.read_bytes() == original
    assert "另一个标签" in second.document.last_error
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(tmp_path / "另存.mindmap"), ""))
    assert window.save(save_as=True)
    assert second.files.path.suffix == ".mindmap" and not second.document.last_error
    assert load_document(second.files.path).root.text == "第二份修改"


@pytest.mark.parametrize("suffix", ["", ".mindmap", ".MINDMAP"])
def test_new_and_save_as_use_native_format_and_normalize_suffix(empty_window, tmp_path, monkeypatch, suffix):
    """首次保存和另存为共用扩展名规则，外部筛选名称不能改变实际原生编码。"""
    window = empty_window
    initial = tmp_path / ("初次保存" + suffix)
    session = create(window, initial)
    expected = initial if suffix else initial.with_suffix(".mindmap")
    assert session.files.path == expected
    assert load_document(expected, format_id="mindmap") == session.engine.document
    original = expected.read_bytes()
    session.engine.update_node(session.engine.root_id, text="另存后的内容")
    target = tmp_path / ("另存目标" + suffix)
    dialogs, writes = [], []

    def choose(*args):
        """模拟对话框返回废弃筛选，控制器也只能使用固定的原生编码。"""
        dialogs.append(args[1:])
        return str(target), "JSON 文档 (*.json)"

    def writer(document, path, **options):
        """记录真正交给写入器的路径及格式，验证补齐在路径占用检查之前完成。"""
        writes.append((path, options["format_id"]))
        save_document(document, path, **options)

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose)
    session.files.writer = writer
    assert window.save(save_as=True)
    output = target if suffix else target.with_suffix(".mindmap")
    assert dialogs == [("保存导图", str(expected), "思维导图 (*.mindmap)")]
    assert writes == [(output, "mindmap")]
    assert session.files.path == output and not session.files.is_dirty
    assert load_document(output, format_id="mindmap") == session.engine.document
    assert expected.read_bytes() == original


@pytest.mark.parametrize("suffix", [".json", ".md", ".txt", ".bin", ".png", ".svg", ".pdf"])
def test_new_and_save_as_reject_other_suffixes_without_writing(empty_window, tmp_path, monkeypatch, suffix):
    """手工后缀不能绕过原生筛选，已有同名文件和现有会话始终保留。"""
    window = empty_window
    session = create(window, tmp_path / "保留.mindmap")
    original_path = session.files.path
    original = original_path.read_bytes()
    session.engine.update_node(session.engine.root_id, text="保留内存修改")
    session.document.timer.stop()
    target = tmp_path / ("错误保存" + suffix)
    target.write_bytes(b"original")
    before, revision = session.engine.document, session.engine.revision
    monkeypatch.setattr("workbench_plugin_mindmap.documents.save_document", lambda *args, **kwargs: pytest.fail("不能写入"))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(target), "思维导图 (*.mindmap)"))
    assert not window.new_document(target, provider_id="mindmap.editor")
    assert not window.new_document()
    assert not window.save(save_as=True)
    assert "仅支持保存为 .mindmap 文件" in session.document.last_error
    assert list(window.plugins.instance("mindmap").sessions.values()) == [session] and window.plugins.instance("mindmap").active_session is session
    assert session.files.path == original_path and session.files.is_dirty
    assert session.engine.document == before and session.engine.revision == revision
    assert original_path.read_bytes() == original and target.read_bytes() == b"original"
    assert not target.with_suffix(".mindmap").exists()


@pytest.mark.parametrize("operation", ["save", "autosave", "save_all"])
@pytest.mark.parametrize("dirty", [False, True])
def test_bound_non_native_path_cannot_bypass_save_validation(empty_window, tmp_path, monkeypatch, operation, dirty):
    """即使外部错误绑定路径，手动、自动和批量保存也不能跳过检查或调用写入器。"""
    window = empty_window
    session = create(window, tmp_path / "原路径.mindmap")
    original_path = session.files.path
    original = original_path.read_bytes()
    recent = list(window.state.recent_files)
    if dirty:
        session.engine.update_node(session.engine.root_id, text="等待保存")
    target = tmp_path / "非法绑定.json"
    target.write_bytes(b"untouched")
    session.files.writer = lambda *args, **kwargs: pytest.fail("不能调用写入器")
    session.files.path = target
    try:
        call = {"save": window.save, "autosave": session.document.autosave, "save_all": window.save_all}[operation]
        assert not call()
        assert session.files.path == target and session.files.is_dirty == dirty
        assert "仅支持保存为 .mindmap 文件" in session.document.last_error
        assert target.read_bytes() == b"untouched" and original_path.read_bytes() == original
        assert window.plugins.instance("mindmap").active_session is session and len(window.plugins.instance("mindmap").sessions) == 1
        assert window.state.recent_files == recent
    finally:
        # 恢复测试故意破坏的绑定，避免退出时的保存检查阻止测试窗口销毁。
        session.files.path = original_path


def test_new_suffix_completion_checks_other_session_ownership(empty_window, tmp_path):
    """省略后缀的目标必须先补齐，再检查是否已经被其他会话占用。"""
    session = create(empty_window, tmp_path / "占用.mindmap")
    original = session.files.path.read_bytes()
    assert not empty_window.new_document(session.files.path.with_suffix(""))
    assert len(empty_window.plugins.instance("mindmap").sessions) == 1 and empty_window.plugins.instance("mindmap").active_session is session
    assert session.files.path.read_bytes() == original


def test_save_as_cancel_keeps_pending_autosave(empty_window, tmp_path, monkeypatch, qtbot):
    """取消另存为不应停止原文件已经排队的自动保存，也不改变会话路径。"""
    session = create(empty_window, tmp_path / "取消另存为.mindmap")
    session.engine.update_node(session.engine.root_id, text="继续自动保存")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: ("", ""))
    assert not empty_window.save(save_as=True) and session.document.timer.isActive()
    qtbot.waitUntil(lambda: not session.files.is_dirty)
    assert load_document(session.files.path).root.text == "继续自动保存"


@pytest.mark.parametrize("suffix", [".json", ".md", ".txt", ".bin", ""])
def test_non_native_open_is_rejected_before_allocating_or_committing(empty_window, tmp_path, monkeypatch, suffix):
    """拒绝扩展名发生在会话分配和输入提交前，已有修改、选择和草稿均保留。"""
    window = empty_window
    first = create(window, tmp_path / "已有.mindmap")
    first.engine.add_child(first.engine.root_id, "保留修改")
    first.document.timer.stop()
    first.canvas.begin_edit(first.engine.root_id)
    first.canvas.editor.setPlainText("未提交的正文")
    source = tmp_path / ("其他文件" + suffix)
    # 使用真正的原生容器也不能绕过扩展名限制，拒绝操作不能改写输入文件。
    save_document(MindMapDocument.new("其他导图"), source, format_id="mindmap")
    original = source.read_bytes()
    before, revision = first.engine.document, first.engine.revision
    selection = first.canvas.selected_node_ids()
    monkeypatch.setattr("workbench_plugin_mindmap.editor_provider.MindMapEditorView", lambda *args: pytest.fail("拒绝前不能分配会话"))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: pytest.fail("不再询问导入另存目标"))
    for operation in (window.plugins.instance("mindmap").sessions.open_path, first.files.open_path):
        with pytest.raises(MindMapError, match="仅支持打开 .mindmap"):
            operation(source)
    assert window.plugins.instance("mindmap").active_session is first and len(window.plugins.instance("mindmap").sessions) == 1
    assert first.engine.document == before and first.engine.revision == revision and first.files.is_dirty
    assert first.canvas.selected_node_ids() == selection and first.canvas.editor.toPlainText() == "未提交的正文"
    assert source.read_bytes() == original


@pytest.mark.parametrize("suffix", [".json", ".md", ".txt"])
def test_renamed_non_native_content_cannot_open(empty_window, tmp_path, suffix):
    """额外格式改名为 .mindmap 仍必须通过原生容器校验，失败不创建标签。"""
    first = create(empty_window, tmp_path / "原有.mindmap")
    source = tmp_path / ("非原生" + suffix)
    save_document(MindMapDocument.new("其他格式"), source)
    target = tmp_path / "伪装.mindmap"
    target.write_bytes(source.read_bytes())
    with pytest.raises(MindMapError):
        empty_window.open_path(target)
    assert empty_window.plugins.instance("mindmap").active_session is first and len(empty_window.plugins.instance("mindmap").sessions) == 1
    assert first.engine.document.root.text == "中心主题"


def test_open_dialog_works_without_session_and_cancel_preserves_active(empty_window, tmp_path, monkeypatch):
    """打开入口不依赖活动控制器；取消不创建候选会话，也不替换已有内容。"""
    source = tmp_path / "从空白页打开.mindmap"
    save_document(MindMapDocument.new("已有文件"), source)
    dialogs = []

    def choose(*args):
        dialogs.append(args[1:])
        return str(source), "思维导图 (*.mindmap)"

    monkeypatch.setattr(QFileDialog, "getOpenFileName", choose)
    assert empty_window.open_dialog()
    assert dialogs == [("打开文件", "", "支持的文件 (*.mindmap *.txt *.md);;导图 (*.mindmap);;文本 (*.txt *.md)")]
    session = empty_window.plugins.instance("mindmap").active_session
    assert session.engine.document.root.text == "已有文件"
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: ("", ""))
    assert not empty_window.open_dialog()
    assert empty_window.plugins.instance("mindmap").active_session is session and len(empty_window.plugins.instance("mindmap").sessions) == 1


def test_open_dialog_and_old_format_parameter_cannot_bypass_native_limit(empty_window, tmp_path, monkeypatch):
    """即使对话框返回额外类型或调用旧参数，也不能越过文件打开边界。"""
    first = create(empty_window, tmp_path / "当前.mindmap")
    source = tmp_path / "额外.json"
    save_document(MindMapDocument.new("JSON 内容"), source)
    selections = []

    def choose(*args):
        selections.append(args[3])
        return str(source), "JSON 文档 (*.json)"

    monkeypatch.setattr(QFileDialog, "getOpenFileName", choose)
    with pytest.raises(ValueError, match="没有启用的编辑器支持"):
        empty_window.open_dialog()
    with pytest.raises(MindMapError, match="仅支持打开 .mindmap"):
        first.files.open_dialog()
    for operation in (empty_window.open_path, empty_window.plugins.instance("mindmap").sessions.open_path, first.files.open_path):
        with pytest.raises(TypeError):
            operation(source, format_id="json")
    assert selections == ["支持的文件 (*.mindmap *.txt *.md);;导图 (*.mindmap);;文本 (*.txt *.md)", "思维导图 (*.mindmap)"]
    assert empty_window.plugins.instance("mindmap").active_session is first and len(empty_window.plugins.instance("mindmap").sessions) == 1


def drop_file_urls(target, urls, *, actions=Qt.DropAction.CopyAction | Qt.DropAction.MoveAction):
    """从窗口入口投递完整拖放序列，由 Qt 命中子控件；Shift 模拟来源建议移动。"""
    mime = QMimeData()
    mime.setUrls(urls)
    window = QWidget.window(target)
    position = QPointF(target.mapTo(window, target.rect().center()))
    args = (position, actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
    for event in (QDragEnterEvent(*args), QDragMoveEvent(*args), QDropEvent(*args)):
        QApplication.sendEvent(window.windowHandle(), event)
        if not event.isAccepted():
            return False
        assert event.dropAction() == Qt.DropAction.CopyAction
    return True


@pytest.mark.parametrize("drop_target", ["welcome", "canvas", "tabs", "search"])
def test_file_drop_opens_native_document_from_main_window(empty_window, tmp_path, qtbot, drop_target):
    """验证主窗口不同落点及文件名解码，能接收文本的搜索框也应打开导图。"""
    window = empty_window
    group = window.host.editor.groups[0]
    previous = None
    if drop_target == "welcome":
        target = group.empty_widget
    else:
        previous = create(window, tmp_path / "原有.mindmap")
        if drop_target == "canvas":
            target = previous.canvas.view.viewport()
        elif drop_target == "tabs":
            target = group.tabBar()
        else:
            window.manager.activate("inspiration", focus=False)
            target = previous.inspiration.search
            target.setText("原搜索内容")
    QApplication.processEvents()
    assert target.isVisible()
    source = tmp_path / "旅行 计划 #1.MINDMAP"
    save_document(MindMapDocument.new("拖入的根主题"), source, format_id="mindmap")
    original = source.read_bytes()
    urls = [QUrl.fromLocalFile(str(source))]

    assert not drop_file_urls(target, urls, actions=Qt.DropAction.MoveAction)
    assert drop_file_urls(target, urls)
    assert window.plugins.instance("mindmap").active_session is previous
    qtbot.waitUntil(lambda: window.plugins.instance("mindmap").active_session is not previous)
    assert window.plugins.instance("mindmap").active_session.files.path == source.resolve()
    assert window.plugins.instance("mindmap").engine.document.root.text == "拖入的根主题"
    assert window.state.recent_files[0] == str(source.resolve())
    assert source.read_bytes() == original
    if drop_target == "search":
        assert target.text() == "原搜索内容"


def test_file_drop_filters_batch_and_reuses_tabs(empty_window, tmp_path, qtbot, monkeypatch):
    """批次内逐项打开，失败保留现有文档，最后一项重复打开时激活原标签。"""
    window = empty_window
    original = create(window, tmp_path / "原有.mindmap")
    original.engine.update_node(original.engine.root_id, text="尚未保存的编辑")
    original.document.timer.stop()
    tab = window.host.editor.find_token(original.id)
    first, last = tmp_path / "第一份.mindmap", tmp_path / "最后一份.mindmap"
    for path in (first, last):
        save_document(MindMapDocument.new(path.stem), path)
    broken, missing = tmp_path / "损坏.mindmap", tmp_path / "缺失.mindmap"
    broken.write_bytes(b"invalid")
    directory, other = tmp_path / "目录.mindmap", tmp_path / "其他类型.txt"
    directory.mkdir()
    save_document(MindMapDocument.new("其他类型"), other, format_id="mindmap")
    items = (first, directory, broken, other, missing, last, original.files.path)
    urls = [QUrl.fromLocalFile(str(path)) for path in items]
    urls.extend([QUrl("https://example.test/导图.mindmap"), QUrl("file:相对路径.mindmap")])
    attempted = []
    open_path = window.open_path

    def track_open(path):
        attempted.append(path)
        return open_path(path)

    monkeypatch.setattr(window, "open_path", track_open)
    assert drop_file_urls(window.title_bar, urls)
    qtbot.waitUntil(lambda: len(attempted) == 6)
    assert attempted == [first, broken, other, missing, last, original.files.path]
    assert [view.files.path for view in window.plugins.instance("mindmap").sessions.ordered()] == [original.files.path, first, last]
    assert window.host.editor.find_token(original.id) == tab and window.plugins.instance("mindmap").active_session is original
    assert original.files.is_dirty and original.engine.document.root.text == "尚未保存的编辑"
    assert window.state.recent_files == [str(original.files.path), str(last), str(first)]
    errors = [line for line in window.operation_log.plain_text().splitlines() if "打开导图失败" in line]
    assert len(errors) == 2 and broken.name in errors[0] and missing.name in errors[1]
    assert window.status.currentMessage()


def test_file_drop_cancel_stops_batch_and_allows_retry(empty_window, tmp_path, qtbot, monkeypatch):
    """后台打开取消后保留此前成功项，处理过程中拒绝重入，结束后可重新拖入。"""
    window = empty_window
    first, large, last = [tmp_path / f"{name}.mindmap" for name in ("已打开", "取消打开", "重新拖入")]
    for path in (first, last):
        save_document(MindMapDocument.new(path.stem), path)
    limit = max(first.stat().st_size, last.stat().st_size)
    large.write_bytes(b"0" * (limit + 1))
    last_url = [QUrl.fromLocalFile(str(last))]
    canceled = []

    def cancel_load(path, parent):
        assert parent is window
        assert not drop_file_urls(window.title_bar, last_url)
        canceled.append(path)
        return None

    monkeypatch.setattr(background_loading, "BACKGROUND_OPEN_BYTES", limit)
    monkeypatch.setattr(background_loading, "load_native_in_background", cancel_load)
    assert drop_file_urls(window.title_bar, [QUrl.fromLocalFile(str(path)) for path in (first, large, last)])
    qtbot.waitUntil(lambda: canceled == [large])
    assert [view.files.path for view in window.plugins.instance("mindmap").sessions.ordered()] == [first]
    assert drop_file_urls(window.title_bar, last_url)
    qtbot.waitUntil(lambda: len(window.plugins.instance("mindmap").sessions) == 2)
    assert window.plugins.instance("mindmap").active_session.files.path == last


def test_file_drop_queued_before_window_close_is_discarded(empty_window, tmp_path, monkeypatch):
    """关闭窗口早于延迟回调时，已接收的拖放也不能继续打开文件。"""
    window = empty_window
    source = tmp_path / "关闭窗口时拖入.mindmap"
    save_document(MindMapDocument.new("待打开"), source)
    monkeypatch.setattr(window, "open_path", lambda *args: pytest.fail("窗口关闭后不能打开文件"))
    assert drop_file_urls(window.title_bar, [QUrl.fromLocalFile(str(source))])
    assert window.close()
    QApplication.processEvents()
    assert len(window.plugins.instance("mindmap").sessions) == 0 and not window.state.recent_files


def test_file_drop_load_finishing_after_window_close_is_discarded(empty_window, tmp_path, qtbot, monkeypatch):
    """模拟后台读取期间关闭窗口，迟到的加载结果不能访问已释放的停靠栈。"""
    window = empty_window
    source = tmp_path / "窗口关闭时完成读取.mindmap"
    save_document(MindMapDocument.new("待打开"), source)
    completed = []

    def finish_after_close(path, parent):
        assert parent is window and window.close()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        completed.append((path, window.status.currentMessage()))
        return background_loading.load_validated_document(path)

    monkeypatch.setattr(background_loading, "BACKGROUND_OPEN_BYTES", 0)
    monkeypatch.setattr(background_loading, "load_native_in_background", finish_after_close)
    assert drop_file_urls(window.title_bar, [QUrl.fromLocalFile(str(source))])
    qtbot.waitUntil(lambda: not window._opening_dropped_files)
    assert completed == [(source, window.status.currentMessage())]
    assert len(window.plugins.instance("mindmap").sessions) == 0 and not window.state.recent_files


def test_cli_explicit_file_creates_session_with_empty_startup(tmp_path):
    """启动参数必须通过会话管理器打开文件，不能依赖启动前已经存在画布。"""
    source = tmp_path / "命令行.mindmap"
    save_document(MindMapDocument.new("命令行打开"), source)
    output = tmp_path / "窗口.png"
    result = subprocess.run(
        [sys.executable, "-m", "personal_workbench", str(source), "--state-dir", str(tmp_path / "命令行配置"),
         "--no-restore", "--theme", "dark", "--screenshot", str(output)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert output.stat().st_size > 1000
    assert json.loads(output.with_suffix(".json").read_text("utf-8"))["theme"] == "dark"


def test_tab_and_split_focus_route_independent_panels_history_and_views(empty_window, tmp_path, qtbot):
    window = empty_window
    first = create(window, tmp_path / "甲.mindmap")
    # 初始定位在显示布局完成后执行，先完成首次显示再模拟导航定位及缩放操作。
    first.canvas.view.scale(0.4, 0.4)
    qtbot.waitUntil(lambda: assert_initial_canvas(first.canvas))
    branch = first.engine.add_child(first.engine.root_id, "甲的分支")
    first.engine.set_expanded(first.engine.root_id, False)
    first.canvas.navigate_to(None, branch)
    first.canvas.view.scale(1.3, 1.3)
    first.canvas.set_editable(False)
    transform = first.canvas.view.transform()
    second = create(window, tmp_path / "乙.mindmap")
    assert not second.engine.can_undo and second.canvas.editable
    window.host.editor.split_token(second.id, Qt.Orientation.Horizontal)
    window.activateWindow()
    first.canvas.view.setFocus()
    qtbot.waitUntil(lambda: window.plugins.instance("mindmap").active_session is first)
    assert window.plugins.instance("mindmap").read_only_action.isChecked()
    assert window.plugins.instance("mindmap")._outline_stack.currentWidget() is first.panels.outline
    second.canvas.view.setFocus()
    qtbot.waitUntil(lambda: window.plugins.instance("mindmap").active_session is second)
    assert not window.plugins.instance("mindmap").read_only_action.isChecked()
    field = second.panels.properties.fields["style.font_size"]
    field.setValue(26)
    field.lineEdit().setFocus()
    QApplication.processEvents()
    assert window.plugins.instance("mindmap").active_session is second
    window.manager.activate_document(first.id)
    assert second.engine.document.root.style["font_size"] == 26
    assert first.engine.document.root.style.get("font_size") != 26
    assert first.canvas.view.transform() == transform and first.canvas.focus_id == branch
    assert first.engine.can_undo and not first.canvas.editable
    window.manager.activate_document(second.id)
    menu = window.main_menu()
    assert menu.actions()[1].menu().actions()[0] is window.action("app.undo")
    assert window.action("app.undo").isEnabled() == second.canvas.action("undo").isEnabled()
    assert window.plugins.instance("mindmap").files is second.files and window.plugins.instance("mindmap").panels is second.panels
    menu.deleteLater()


def test_switch_commits_inline_input_without_returning_focus_to_old_split(empty_window, tmp_path, qtbot):
    window = empty_window
    first = create(window, tmp_path / "甲.mindmap")
    second = create(window, tmp_path / "乙.mindmap")
    window.host.editor.split_token(second.id, Qt.Orientation.Horizontal)
    window.manager.activate_document(first.id)
    first.canvas.begin_edit(first.engine.root_id)
    first.canvas.editor.setPlainText("提交给甲")
    second.canvas.view.setFocus()
    qtbot.waitUntil(lambda: window.plugins.instance("mindmap").active_session is second and first.canvas.editor is None)
    QApplication.processEvents()
    assert second.canvas.view.hasFocus() and window.plugins.instance("mindmap").active_session is second
    assert first.engine.document.root.text == "提交给甲"
    assert second.engine.document.root.text == "中心主题"


def test_outline_depth_is_per_session_and_preferences_are_shared(empty_window, tmp_path):
    window = empty_window
    first = create(window, tmp_path / "甲.mindmap")
    first.panels.outline.set_display_depth(4)
    second = create(window, tmp_path / "乙.mindmap")
    assert second.panels.outline.display_depth == 4
    second.panels.outline.set_display_depth(1)
    second.canvas.set_wheel_mode("zoom")
    second.canvas.set_edraw_paste_options("replace", False, False)
    assert first.canvas.wheel_mode == "zoom" and first.canvas.edraw_paste_options == ("replace", False, False)
    assert first.panels.outline.display_depth == 4
    window.manager.activate_document(first.id)
    assert window.plugins.instance("mindmap")._depth_action.menu() is first.panels.outline.depth_menu
    third = create(window, tmp_path / "丙.mindmap")
    assert third.panels.outline.display_depth == 1
    assert third.canvas.wheel_mode == "zoom" and third.canvas.edraw_paste_options == ("replace", False, False)


def test_autosave_debounce_undo_redo_and_view_only_changes(empty_window, tmp_path, qtbot):
    session = create(empty_window, tmp_path / "自动.mindmap")
    writes = []

    def writer(document, path, **options):
        writes.append(document.root.text)
        return save_document(document, path, **options)

    session.files.writer = writer
    session.engine.update_node(session.engine.root_id, text="第一次")
    qtbot.wait(270)
    session.engine.update_node(session.engine.root_id, text="第二次")
    qtbot.wait(270)
    assert not writes and session.files.is_dirty
    qtbot.waitUntil(lambda: len(writes) == 1)
    assert writes == ["第二次"] and not session.files.is_dirty
    assert session.engine.undo()
    qtbot.waitUntil(lambda: len(writes) == 2)
    assert load_document(session.files.path).root.text == "第一次"
    assert session.engine.redo()
    qtbot.waitUntil(lambda: len(writes) == 3)
    session.canvas.view.scale(1.1, 1.1)
    session.canvas.center_root()
    session.canvas.select_nodes([session.engine.root_id])
    qtbot.wait(550)
    assert len(writes) == 3 and not session.document.timer.isActive()


@pytest.mark.parametrize("operation", ["style", "layout", "collapse", "theme"])
def test_document_changes_schedule_committed_save(empty_window, tmp_path, operation, qtbot):
    session = create(empty_window, tmp_path / "内容.mindmap")
    engine = session.engine
    if operation == "style":
        engine.update_node(engine.root_id, style={"font_size": 25})
    elif operation == "layout":
        engine.set_layout(LayoutConfig(direction="left"))
    elif operation == "collapse":
        engine.add_child(engine.root_id, "子主题")
        engine.set_expanded(engine.root_id, False)
    else:
        engine.set_theme(Theme.dark())
    assert session.document.timer.isActive()
    qtbot.waitUntil(lambda: not session.files.is_dirty)
    assert load_document(session.files.path) == engine.document


def test_external_fold_button_autosaves_and_undo_redo_restore_saved_state(empty_window, tmp_path, qtbot):
    """真实按钮点击经既有防抖保存落盘，撤销重做也保存各自的折叠状态。"""
    session = create(empty_window, tmp_path / "按钮折叠.mindmap")
    engine, canvas = session.engine, session.canvas
    engine.add_child(engine.root_id, "子主题")
    qtbot.waitUntil(lambda: not session.files.is_dirty)
    canvas.select_nodes([engine.root_id])
    canvas.center_root()
    QApplication.processEvents()
    control = canvas.node_items[engine.root_id].fold_control
    position = canvas.view.mapFromScene(control.mapToScene(control.button_rect().center()))
    qtbot.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=position)
    assert not engine.get_node(engine.root_id).expanded and session.document.timer.isActive()
    qtbot.waitUntil(lambda: not session.files.is_dirty)
    assert not load_document(session.files.path).root.expanded
    assert engine.undo()
    qtbot.waitUntil(lambda: not session.files.is_dirty)
    assert load_document(session.files.path).root.expanded
    assert engine.redo()
    qtbot.waitUntil(lambda: not session.files.is_dirty)
    assert not load_document(session.files.path).root.expanded


def test_autosave_keeps_inline_ime_and_property_draft(empty_window, tmp_path):
    session = create(empty_window, tmp_path / "不打断输入.mindmap")
    session.engine.update_node(session.engine.root_id, text="已提交")
    session.canvas.begin_edit(session.engine.root_id)
    editor = session.canvas.editor
    editor.setPlainText("尚未提交的正文")
    event = QInputMethodEvent("输入法候选", [])
    editor.inputMethodEvent(event)
    assert session.document.autosave()
    assert session.canvas.editor is editor and editor.composing
    assert load_document(session.files.path).root.text == "已提交"
    assert session.engine.can_undo and not session.files.is_dirty
    assert not session.document.save()
    editor.inputMethodEvent(QInputMethodEvent())
    session.canvas.finish_edit(commit=False)
    panel = session.panels.properties
    panel.fields["style.font_size"].setValue(28)
    panel.timer.stop()
    assert session.document.autosave()
    assert panel._pending == {"style.font_size": 28}
    assert load_document(session.files.path).root.style.get("font_size") != 28
    assert session.document.save() and load_document(session.files.path).root.style["font_size"] == 28


def test_saved_notification_keeps_open_appearance_menu(empty_window, tmp_path):
    """自动保存只同步文件状态，不关闭正在使用的应用设置菜单或重建状态栏按钮。"""
    session = create(empty_window, tmp_path / "菜单.mindmap")
    session.engine.add_child(session.engine.root_id, "修改")
    menu = empty_window.appearance_menu()
    menu.popup(empty_window.mapToGlobal(empty_window.rect().center()))
    QApplication.processEvents()
    assert menu.isVisible()
    wheel = empty_window.plugins.instance("mindmap")._status_controls.buttons["wheel_mode"]
    assert session.document.autosave()
    assert menu.isVisible() and empty_window.plugins.instance("mindmap")._status_controls.buttons["wheel_mode"] is wheel
    menu.close()
    menu.deleteLater()


def test_save_failure_preserves_file_and_retries_only_on_request_or_edit(empty_window, tmp_path, qtbot):
    session = create(empty_window, tmp_path / "失败.mindmap")
    original = session.files.path.read_bytes()
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise OSError("模拟磁盘写入失败")

    session.files.writer = fail
    session.engine.update_node(session.engine.root_id, text="保留内存修改")
    qtbot.waitUntil(lambda: bool(calls))
    assert session.files.is_dirty and "磁盘" in session.document.last_error
    assert session.files.path.read_bytes() == original and not session.document.timer.isActive()
    qtbot.wait(600)
    assert len(calls) == 1
    group, index = empty_window.host.editor.find_token(session.id)
    assert group.tabText(index).endswith(" *") and "保存失败" in group.tabToolTip(index)
    assert not empty_window.manager.close_document(session.id)
    assert session.id in empty_window.manager.documents and not session.disposed
    calls_before_undo = len(calls)
    assert session.engine.undo()
    qtbot.waitUntil(lambda: not session.document.last_error)
    assert not session.files.is_dirty and len(calls) == calls_before_undo
    assert session.files.path.read_bytes() == original
    session.files.writer = session.document._write
    session.engine.update_node(session.engine.root_id, text="后续修改重试")
    qtbot.waitUntil(lambda: not session.files.is_dirty)
    assert not session.document.last_error and load_document(session.files.path).root.text == "后续修改重试"


def test_save_all_order_failure_and_atomic_close_others(empty_window, tmp_path):
    window = empty_window
    sessions = [create(window, tmp_path / f"{name}.mindmap") for name in ("甲", "乙", "丙")]
    group, _ = window.host.editor.find_token(sessions[0].id)
    group.tabBar().moveTab(2, 0)
    writes = []
    for session in sessions:
        session.engine.update_node(session.engine.root_id, text=session.files.path.stem)
        session.document.timer.stop()

        def writer(document, path, **options):
            writes.append(path.stem)
            if path.stem == "乙":
                raise OSError("乙无法保存")
            save_document(document, path, **options)

        session.files.writer = writer
    window.manager.activate_document(sessions[0].id)
    assert not window.save_all() and writes == ["丙", "甲", "乙"]
    assert window.plugins.instance("mindmap").active_session is sessions[1] and len(window.plugins.instance("mindmap").sessions) == 3
    group.close_others(sessions[0].id)
    assert len(window.plugins.instance("mindmap").sessions) == 3 and not any(session.disposed for session in sessions)
    sessions[1].files.writer = sessions[1].document._write
    assert window.save_all() and window.plugins.instance("mindmap").active_session is sessions[1]
    group.close_others(sessions[0].id)
    assert list(window.plugins.instance("mindmap").sessions.values()) == [sessions[0]]
    assert window.plugins.instance("mindmap").active_session is sessions[0]


def test_exit_failure_keeps_all_targets_and_success_releases_resources(empty_window, tmp_path):
    window = empty_window
    first = create(window, tmp_path / "甲.mindmap")
    second = create(window, tmp_path / "乙.mindmap")
    second.engine.update_node(second.engine.root_id, text="关闭前修改")
    second.document.timer.stop()

    def fail(*args, **kwargs):
        raise OSError("退出保存失败")

    second.files.writer = fail
    assert not window.close() and len(window.plugins.instance("mindmap").sessions) == 2
    assert window.plugins.instance("mindmap").active_session is second and not first.disposed and not second.disposed
    second.files.writer = second.document._write
    assert window.close() and first.disposed and second.disposed
    assert not first.document.timer.isActive() and not second.document.timer.isActive()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(first.canvas) and not isValid(second.panels.properties)


def test_layout_restore_preserves_live_documents_and_restart_reopens_files(empty_window, tmp_path, qtbot):
    window = empty_window
    first = create(window, tmp_path / "甲.mindmap")
    snapshot = window.manager.capture_layout()
    second = create(window, tmp_path / "乙.mindmap")
    window.host.editor.split_token(second.id, Qt.Orientation.Horizontal)
    window.manager.activate_document(first.id)
    for layout in (snapshot, None):
        before = deepcopy(layout)
        if layout is None:
            window.manager.restore_default_layout()
        else:
            window.manager.restore_layout(layout)
        assert layout == before and window.plugins.instance("mindmap").active_session is first
        assert all(window.host.editor.find_token(session.id) for session in (first, second))
        group, index = window.host.editor.find_token(second.id)
        assert group.tabToolTip(index) == str(second.files.path)
    # 真实快捷键使用同一保留策略，不允许默认模板中的空标签组隐藏运行中会话。
    window.activateWindow()
    first.canvas.view.setFocus()
    QApplication.processEvents()
    qtbot.keyClick(first.canvas.view, Qt.Key.Key_F12, Qt.KeyboardModifier.ShiftModifier)
    assert window.plugins.instance("mindmap").active_session is first
    assert all(window.host.editor.find_token(session.id) for session in (first, second))
    window.manager.save_session()
    text = window.manager.storage_file.read_text("utf-8")
    assert str(first.files.path) not in text and "甲.mindmap" not in text
    assert set(json.loads(text)) == {"schema_version", "session"}
    storage = window.manager.storage_dir
    assert window.close()
    restarted = build_window(storage_dir=storage)
    qtbot.addWidget(restarted)
    assert restarted.plugins.instance("mindmap").active_session.id == first.id
    assert set(restarted.manager.documents) == {first.id, second.id}
    assert restarted.plugins.instance("mindmap").sessions.get(first.id).files.path == first.files.path
    assert restarted.plugins.instance("mindmap").sessions.get(second.id).files.path == second.files.path
    assert all(restarted.host.editor.find_token(session.id) for session in (first, second))
    restarted.close()


def test_save_all_shortcut_from_sidebar_preserves_active_session(empty_window, tmp_path, qtbot):
    window = empty_window
    first = create(window, tmp_path / "甲.mindmap")
    second = create(window, tmp_path / "乙.mindmap")
    first.engine.update_node(first.engine.root_id, text="保存全部")
    first.document.timer.stop()
    window.activateWindow()
    field = second.panels.properties.fields["style.font_size"]
    field.lineEdit().setFocus()
    field.setValue(24)
    QApplication.processEvents()
    qtbot.keyClick(field.lineEdit(), Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
    assert not first.files.is_dirty and not second.files.is_dirty
    assert load_document(first.files.path).root.text == "保存全部"
    assert load_document(second.files.path).root.style["font_size"] == 24
    assert window.plugins.instance("mindmap").active_session is second and field.lineEdit().hasFocus()


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("page_kind", ["submap", "detail"])
def test_owned_pages_are_isolated_between_sessions_and_reopen_from_main(empty_window, tmp_path, split, page_kind):
    """标签和分屏仅切换所属画布，导航现场独立，文件重开从主导图开始。"""
    window = empty_window
    first = create(window, tmp_path / "子导图甲.mindmap")
    owner = first.engine.add_child(first.engine.root_id, "甲的入口")
    first.canvas.select_nodes([owner])
    create_page = "create_submap" if page_kind == "submap" else "create_detail_page"
    enter_page = "enter_submap" if page_kind == "submap" else "enter_detail_page"
    page = getattr(first.canvas, create_page)()
    QApplication.processEvents()
    root = first.canvas.document_view.root_id
    first.engine.add_child(root, "甲的页内内容")
    first.canvas.view.zoom(1.4)
    scale = first.canvas.view.transform().m11()
    second = create(window, tmp_path / "子导图乙.mindmap")
    if split:
        window.host.editor.split_token(second.id, Qt.Orientation.Horizontal)
    second.canvas.select_nodes([second.engine.root_id])
    other = getattr(second.canvas, create_page)()
    second.engine.add_child(second.canvas.document_view.root_id, "乙的页内内容")
    assert window.save_all()
    assert load_document(first.files.path) == first.engine.document
    assert load_document(second.files.path) == second.engine.document
    window.manager.activate_document(first.id)
    QApplication.processEvents()
    assert first.canvas.current_page_id == page and second.canvas.current_page_id == other
    assert first.canvas.view.transform().m11() == scale
    assert window.plugins.instance("mindmap").panels.outline.tree_model.document.root_id == root
    revision = first.engine.revision
    first.canvas.return_to_parent_page()
    getattr(first.canvas, enter_page)(owner)
    assert first.engine.revision == revision and not first.engine.is_dirty
    path = first.files.path
    assert window.manager.close_document(first.id)
    assert window.open_path(path)
    reopened = window.plugins.instance("mindmap").active_session
    assert reopened.canvas.current_page_id is None
    assert reopened.engine.document.has_page(page)
    assert reopened.canvas.document_view.root_id == reopened.engine.root_id
    assert second.canvas.current_page_id == other


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("page_kind", ["main", "submap", "detail"])
def test_clipboard_image_paste_isolates_sessions_and_survives_file_lifecycle(
    empty_window, tmp_path, image_clipboard, qtbot, monkeypatch, split, page_kind,
):
    """真实粘贴与缩放在所属页保存，历史、另存为和关闭重开保持尺寸及资源独立。"""
    window = empty_window
    original_path = tmp_path / "图片甲.mindmap"
    first = create(window, original_path)
    owner = first.engine.add_child(first.engine.root_id, "附图位置")
    first.canvas.select_nodes([owner])
    if page_kind == "submap":
        first.canvas.create_submap()
    elif page_kind == "detail":
        first.canvas.create_detail_page()
    page, target = first.canvas.current_page_id, first.canvas.current_node_id()
    second = create(window, tmp_path / "图片乙.mindmap")
    if split:
        window.host.editor.split_token(second.id, Qt.Orientation.Horizontal)
    assert window.save_all()
    other_before = second.engine.document
    other_revision = second.engine.revision
    window.manager.activate_document(first.id)
    window.activateWindow()
    first.canvas.view.setFocus()
    qtbot.waitUntil(lambda: window.plugins.instance("mindmap").active_session is first)
    QApplication.processEvents()
    first.canvas.view.zoom(1.3)
    transform = first.canvas.view.transform()
    image = QImage(40, 30, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.red)
    mime = QMimeData()
    mime.setImageData(image)
    image_clipboard.setMimeData(mime)
    qtbot.keyClick(first.canvas.view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    image_node = first.canvas.selected_node_ids()[0]
    assert first.engine.parent_id(image_node) == target
    asset = first.engine.get_node(image_node).image_id
    assert asset is not None and first.canvas.current_page_id == page
    assert first.canvas.view.transform() == transform and first.files.is_dirty
    qtbot.waitUntil(lambda: not first.files.is_dirty)
    assert load_document(original_path) == first.engine.document
    assert second.engine.document == other_before and second.engine.revision == other_revision
    assert not second.files.is_dirty

    destination = tmp_path / "图片甲副本.mindmap"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(destination), "原生导图"))
    assert window.save(save_as=True) and first.files.path == destination
    expected = first.engine.document
    assert load_document(destination) == expected
    first.canvas.navigate_to(None)
    assert first.engine.undo()
    assert first.canvas.current_page_id == page
    assert image_node not in first.engine.document.nodes
    qtbot.waitUntil(lambda: not first.files.is_dirty)
    assert asset not in load_document(destination).assets
    assert asset in load_document(original_path).assets
    assert first.engine.redo()
    qtbot.waitUntil(lambda: not first.files.is_dirty)
    assert load_document(destination) == expected

    # 视口事件已由画布测试逐手柄覆盖，这里验证拖动提交后的工作台会话及保存链。
    item = first.canvas.node_items[image_node]
    resize = first.canvas.view.image_resize
    start = first.canvas.view.mapFromScene(item.mapToScene(item.visual.image_rect.right(),
                                                         item.visual.image_rect.center().y()))
    end = start + QPoint(52, 0)
    resize.begin(3, start)
    resize.update(end)
    assert first.engine.document == expected and not first.files.is_dirty
    resize.finish(end)
    width = first.engine.get_node(image_node).image_width
    assert width == pytest.approx(80) and first.files.is_dirty
    assert second.engine.document == other_before and not second.files.is_dirty
    qtbot.waitUntil(lambda: not first.files.is_dirty)
    assert load_document(destination).nodes[image_node].image_width == pytest.approx(width)
    assert load_document(original_path).nodes[image_node].image_width == 40
    assert first.engine.undo() and first.engine.document == expected
    qtbot.waitUntil(lambda: not first.files.is_dirty)
    assert first.engine.redo() and first.engine.get_node(image_node).image_width == width
    qtbot.waitUntil(lambda: not first.files.is_dirty)
    expected = first.engine.document

    image.fill(Qt.GlobalColor.blue)
    mime = QMimeData()
    mime.setImageData(image)
    image_clipboard.setMimeData(mime)
    first.canvas.set_editable(False)
    first.canvas.view.setFocus()
    qtbot.keyClick(first.canvas.view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert first.engine.document == expected and not first.files.is_dirty
    old_canvas = first.canvas
    assert window.manager.close_document(first.id)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qtbot.waitUntil(lambda: not isValid(old_canvas))
    assert window.open_path(destination)
    reopened = window.plugins.instance("mindmap").active_session
    assert reopened.canvas.current_page_id is None
    assert reopened.engine.document == expected
    assert QImage.fromData(reopened.engine.document.assets[asset].data).pixelColor(5, 5) == Qt.GlobalColor.red
    assert second.engine.document == other_before and not second.files.is_dirty


@pytest.mark.parametrize("page_kind", ["submap", "detail"])
def test_background_snapshot_keeps_owned_pages_and_preserves_newer_page_edit(
    empty_window, tmp_path, monkeypatch, qtbot, controlled_save_executor, page_kind
):
    """后台写入旧版本的全文件快照，期间对子页的修改继续保持未保存。"""
    session = create_large_session(empty_window, tmp_path / "子图后台版本.mindmap")
    # 从独立新叶子创建入口，原大图结构继续覆盖后台保存阈值。
    owner = session.engine.add_child(session.engine.root_id, "后台入口")
    session.canvas.select_nodes([owner])
    page = (session.canvas.create_submap() if page_kind == "submap" else session.canvas.create_detail_page())
    key = session.engine.add_child(session.canvas.document_view.root_id, "捕获版本")
    started, release = Event(), Event()

    def held_writer(document, path, **options):
        started.set()
        assert release.wait(5)
        save_document(document, path, **options)

    monkeypatch.setattr("workbench_plugin_mindmap.documents.save_snapshot", held_writer)
    try:
        session.document.timer.stop()
        assert session.document.autosave(background=True)
        qtbot.waitUntil(started.is_set)
        session.engine.update_node(key, text="较新的页内编辑")
        session.document.timer.stop()
        release.set()
        qtbot.waitUntil(lambda: session.document._background_save is None)
        session.document.timer.stop()
        saved = load_document(session.files.path)
        assert saved.has_page(page) and saved.nodes[key].text == "捕获版本"
        assert session.engine.is_dirty
        assert session.document.autosave()
        assert load_document(session.files.path).nodes[key].text == "较新的页内编辑"
    finally:
        release.set()
        session.document._finish_background_save(wait=True)
