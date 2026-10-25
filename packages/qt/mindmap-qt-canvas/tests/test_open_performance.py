"""大文件按需打开：首帧排版范围、页外错误及扩展调度的回归验证。"""

from copy import deepcopy
from threading import Event, get_ident
from types import SimpleNamespace

import pytest
from mindmap_core import (
    Asset,
    FormatRegistry,
    LayoutConfig,
    LayoutRegistry,
    MindMapDocument,
    MindMapEngine,
    MindMapError,
    Node,
)
from mindmap_core import validation as core_validation
from mindmap_formats import MindmapCodec, create_default_format_registry, save_document
from mindmap_layouts import TreeLayout
from mindmap_qt_canvas import MindMapFileController, MindMapWidget
from mindmap_qt_canvas import loading as background_loading
from mindmap_qt_render import rendering
from PySide6.QtCore import QBuffer, QIODevice, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QProgressDialog, QPushButton, QWidget


def paged_document():
    """直接填充非活动子导图，避免把准备数据的历史通知混入排版计数。"""
    engine = MindMapEngine(MindMapDocument.new("打开后立即显示"))
    owner = engine.add_child(engine.root_id, "子导图入口")
    page_id = engine.create_submap(owner)
    document = engine.document
    root = document.submaps[page_id].root_id
    keys = [root]
    for index in range(320):
        node = Node(f"延迟节点 {index}")
        document.nodes[keys[index // 5]].children.append(node.id)
        document.nodes[node.id] = node
        keys.append(node.id)
    document.validate()
    return document, owner, page_id, keys


@pytest.fixture
def canvas_factory(qtbot):
    """文件控制器仅使用输入提交协议，测试退出时解除画布引擎订阅。"""
    widgets = []

    def create(*, background_open=False, **options):
        canvas = MindMapWidget(**options)
        qtbot.addWidget(canvas)
        widgets.append(canvas)
        return MindMapFileController(canvas, SimpleNamespace(flush_pending_edits=lambda: None), canvas,
                                    background_open=background_open)

    yield create
    for canvas in widgets:
        canvas.dispose()


def test_open_defers_inactive_text_until_navigation_and_preserves_geometry(canvas_factory, tmp_path, monkeypatch):
    document, owner, page_id, keys = paged_document()
    source = tmp_path / "按需打开.mindmap"
    save_document(document, source)
    original_bytes = source.read_bytes()
    files = canvas_factory()
    calls = []
    make_text = rendering.make_text

    def measured(text, *args, **kwargs):
        calls.append(text)
        return make_text(text, *args, **kwargs)

    monkeypatch.setattr(rendering, "make_text", measured)
    assert files.open_path(source)
    assert not any(text.startswith("延迟节点") for text in calls)
    assert files.engine.node_count == len(document.nodes)
    assert not files.is_dirty and not files.engine.can_undo
    files.canvas.enter_submap(owner)
    assert any(text.startswith("延迟节点") for text in calls)
    assert set(files.canvas.layout_result.nodes) == set(keys)
    expected = TreeLayout().layout(document.page_view(page_id), files.canvas._measurements)
    assert dict(files.canvas.layout_result.nodes) == expected.nodes
    assert files.engine.document == document
    assert not files.is_dirty and not files.engine.can_undo
    assert source.read_bytes() == original_bytes


def test_native_open_validates_entire_book_once(canvas_factory, tmp_path, monkeypatch):
    """计数真实全图遍历，确保大量非活动节点不会在多个接口间重复校验。"""
    document, *_ = paged_document()
    source = tmp_path / "全图仅校验一次.mindmap"
    save_document(document, source)
    files = canvas_factory()
    original, counts = core_validation.validate_document, []

    def counted(candidate, **options):
        counts.append(len(candidate.nodes))
        return original(candidate, **options)

    monkeypatch.setattr(core_validation, "validate_document", counted)
    assert files.open_path(source)
    assert counts.count(len(document.nodes)) == 1
    assert files.engine.document == document and not files.is_dirty


def test_background_open_keeps_events_running_and_commits_on_gui_thread(canvas_factory, tmp_path, monkeypatch):
    """线程门闩控制解析完成时点，验证 GUI 有事件响应且禁止控制器重入。"""
    document, *_ = paged_document()
    source = tmp_path / "后台打开.mindmap"
    save_document(document, source)
    files = canvas_factory(background_open=True)
    started, release = Event(), Event()
    gui_thread, worker_threads, event_threads, reentries = get_ident(), [], [], []
    original = background_loading.load_validated_document

    def held_reader(path):
        worker_threads.append(get_ident())
        started.set()
        assert release.wait(5)
        return original(path)

    def gui_tick():
        if started.is_set():
            reentries.append(files.open_path(source))
            release.set()
            timer.stop()

    monkeypatch.setattr(background_loading, "BACKGROUND_OPEN_BYTES", 0)
    monkeypatch.setattr(background_loading, "load_validated_document", held_reader)
    unsubscribe = files.engine.subscribe(lambda event: event_threads.append(get_ident()))
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(gui_tick)
    timer.start()
    try:
        assert files.open_path(source)
        assert reentries == [False] and worker_threads[0] != gui_thread
        assert event_threads == [gui_thread]
        assert files.engine.document == document and files.path == source and not files.is_dirty
    finally:
        release.set()
        timer.stop()
        unsubscribe()


def test_cancel_background_open_discards_late_result(canvas_factory, tmp_path, monkeypatch, qtbot):
    """取消立即返回，稍后完成的工作线程不能替换旧文档、路径或保存状态。"""
    document, *_ = paged_document()
    source = tmp_path / "取消后台打开.mindmap"
    save_document(document, source)
    original_bytes = source.read_bytes()
    files = canvas_factory(background_open=True)
    before, revision = files.engine.document, files.engine.revision
    started, release, finished = Event(), Event(), Event()
    original = background_loading.load_validated_document

    def held_reader(path):
        started.set()
        try:
            assert release.wait(5)
            return original(path)
        finally:
            finished.set()

    def cancel():
        if started.is_set():
            dialog = files.canvas.findChild(QProgressDialog)
            if dialog is not None:
                dialog.findChild(QPushButton).click()
                timer.stop()

    monkeypatch.setattr(background_loading, "BACKGROUND_OPEN_BYTES", 0)
    monkeypatch.setattr(background_loading, "load_validated_document", held_reader)
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(cancel)
    timer.start()
    try:
        assert not files.open_path(source)
        assert not finished.is_set() and not files._opening
        assert files.engine.document == before and files.engine.revision == revision and files.path is None
    finally:
        release.set()
        timer.stop()
    qtbot.waitUntil(finished.is_set)
    assert files.engine.document == before and files.path is None and source.read_bytes() == original_bytes


def test_background_load_failure_cleans_up_and_allows_retry(canvas_factory, tmp_path, monkeypatch):
    files = canvas_factory(background_open=True)
    source = tmp_path / "读取失败后重试.mindmap"
    source.write_bytes(b"invalid archive")
    before = files.engine.document
    monkeypatch.setattr(background_loading, "BACKGROUND_OPEN_BYTES", 0)
    with pytest.raises(MindMapError):
        files.open_path(source)
    assert not files._opening and files.engine.document == before and files.path is None
    assert not any(dialog.isVisible() for dialog in files.canvas.findChildren(QProgressDialog))
    save_document(MindMapDocument.new("重试成功"), source)
    assert files.open_path(source) and files.engine.document.root.text == "重试成功"


def test_background_wait_can_outlive_destroyed_parent(tmp_path, monkeypatch, qtbot):
    """父窗口销毁时结束等待，未完成的读取不再持有或访问任何 Qt 对象。"""
    started, release, finished = Event(), Event(), Event()

    def held_reader(path):
        started.set()
        try:
            assert release.wait(5)
            return None
        finally:
            finished.set()

    monkeypatch.setattr(background_loading, "load_validated_document", held_reader)
    parent = QWidget()
    timer = QTimer()
    timer.setInterval(5)

    def destroy():
        if started.is_set():
            parent.deleteLater()
            timer.stop()

    timer.timeout.connect(destroy)
    timer.start()
    try:
        assert background_loading.load_native_in_background(tmp_path / "占位.mindmap", parent) is None
    finally:
        release.set()
        timer.stop()
    qtbot.waitUntil(finished.is_set)


def test_custom_layout_keeps_trial_isolation_and_full_validation(canvas_factory, tmp_path, monkeypatch):
    """扩展只修改布局副本，原候选保持完整；扩展流程仍须逐层校验后接管。"""
    class MutatingLayout(TreeLayout):
        def layout(self, document, measurements=None, **options):
            result = super().layout(document, measurements, **options)
            if len(document.nodes) > 10:
                document.root.children.append("扩展制造的缺失节点")
            return result

    registry = LayoutRegistry()
    registry.register(MutatingLayout())
    files = canvas_factory(layout_registry=registry)
    document, *_ = paged_document()
    source = tmp_path / "扩展修改候选.mindmap"
    save_document(document, source)
    original, counts = core_validation.validate_document, []

    def counted(candidate, **options):
        counts.append(len(candidate.nodes))
        return original(candidate, **options)

    monkeypatch.setattr(core_validation, "validate_document", counted)
    assert files.open_path(source)
    assert counts.count(len(document.nodes)) >= 3
    assert files.engine.document == document and files.path == source


@pytest.mark.parametrize("invalid", ["layout", "image"])
def test_invalid_inactive_page_is_rejected_before_replacement(canvas_factory, tmp_path, monkeypatch, invalid):
    document, _, page_id, keys = paged_document()
    if invalid == "layout":
        document.submaps[page_id].layout = LayoutConfig(options={"unknown_option": 4})
    else:
        engine = MindMapEngine(document)
        engine.attach_image(keys[-1], b"invalid image")
        document = engine.document
    source = tmp_path / "页外错误.mindmap"
    save_document(document, source)
    files = canvas_factory()
    before = files.engine.document
    confirmations = []
    monkeypatch.setattr(files, "confirm_replace", lambda: confirmations.append(True))
    with pytest.raises(MindMapError):
        files.open_path(source)
    assert not confirmations
    assert files.engine.document == before and files.path is None


def test_custom_layout_still_validates_inactive_pages(canvas_factory, tmp_path):
    class RestrictedLayout(TreeLayout):
        def layout(self, document, measurements=None, **options):
            if any(node.text.startswith("延迟节点") for node in document.nodes.values()):
                raise MindMapError("扩展拒绝非活动页面")
            return super().layout(document, measurements, **options)

    registry = LayoutRegistry()
    registry.register(RestrictedLayout())
    files = canvas_factory(layout_registry=registry)
    document, *_ = paged_document()
    source = tmp_path / "扩展校验.mindmap"
    save_document(document, source)
    before = deepcopy(files.engine.document)
    with pytest.raises(MindMapError, match="扩展拒绝"):
        files.open_path(source)
    assert files.engine.document == before and files.path is None


def test_open_rejects_overflowing_geometry_before_replacement(canvas_factory, tmp_path):
    """增量预检仍须读取摘要边界，避免有限图片尺寸累加溢出后才在场景内失败。"""
    files = canvas_factory()
    image = QImage(1, 1, QImage.Format.Format_RGB32)
    image.fill(0)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    asset = Asset.from_bytes(bytes(buffer.data()))
    document = MindMapDocument.new("尺寸溢出")
    document.assets[asset.id] = asset
    parent = document.root
    for _ in range(3):
        node = Node("", image_id=asset.id, image_width=1e308, metadata={"image_only": True})
        parent.children.append(node.id)
        document.nodes[node.id] = node
        parent = node
    source = tmp_path / "溢出尺寸.mindmap"
    save_document(document, source)
    before = files.engine.document
    with pytest.raises(MindMapError, match="有限数值"):
        files.open_path(source)
    assert files.engine.document == before and files.path is None


def test_extension_reader_result_keeps_independent_ownership(canvas_factory, tmp_path):
    """扩展读取器可以持有自己的文档，打开及后续编辑不能改写扩展仍拥有的来源。"""
    document, *_ = paged_document()

    class RetainingCodec(MindmapCodec):
        def read(self, stream, *, options):
            return document

    registry = FormatRegistry()
    registry.register(create_default_format_registry().get("mindmap").info, importer=RetainingCodec())
    files = canvas_factory(format_registry=registry)
    source = tmp_path / "扩展来源.mindmap"
    save_document(document, source)
    before = deepcopy(document)
    assert files.open_path(source)
    files.engine.update_node(files.engine.root_id, text="正式编辑")
    assert document == before and files.engine.document != before


def test_engine_override_keeps_original_replacement_dispatch(canvas_factory, tmp_path):
    """引擎覆写原有替换入口时，原生快速读取不能绕过该宿主入口。"""
    class TrackingEngine(MindMapEngine):
        replacements = 0

        def replace_document(self, document):
            self.replacements += 1
            return super().replace_document(document)

    engine = TrackingEngine()
    files = canvas_factory(engine=engine)
    source = tmp_path / "引擎扩展.mindmap"
    document, *_ = paged_document()
    save_document(document, source)
    assert files.open_path(source)
    assert engine.replacements == 1 and engine.document == document


def test_engine_document_projection_is_used_for_initialization_and_reset(canvas_factory):
    """引擎扩展的文档投影必须继续影响画布，不被只读快照优化绕过。"""
    class ProjectingEngine(MindMapEngine):
        @property
        def document(self):
            document = super().document
            document.root.note = "扩展提供的显示备注"
            return document

    engine = ProjectingEngine()
    files = canvas_factory(engine=engine)
    assert files.canvas.document_view.root.note == "扩展提供的显示备注"
    engine.replace_document(MindMapDocument.new("新的来源"))
    assert files.canvas.document_view.root.text == "新的来源"
    assert files.canvas.document_view.root.note == "扩展提供的显示备注"
