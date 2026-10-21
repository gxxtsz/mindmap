"""画布与导出共享注入服务，异常配置与损坏剪贴板在修改前拒绝。"""

from copy import deepcopy

import pytest
from mindmap_engine import (
    LayoutConfig,
    LayoutInfo,
    MindMapEngine,
    MindMapError,
    TreeLayout,
    create_default_format_registry,
    create_default_layout_registry,
)
from mindmap_formats import JsonCodec
from mindmap_qt import MindMapWidget
from mindmap_qt.widget import CLIPBOARD_MIME
from PySide6.QtCore import QMimeData
from PySide6.QtWidgets import QApplication


class TrackingLayout(TreeLayout):
    info = LayoutInfo(
        "tracking",
        "跟踪布局",
        LayoutConfig(algorithm_id="tracking"),
        directions=("right",),
        supports_compact=False,
    )

    def __init__(self):
        self.calls = []

    def validate_config(self, config):
        if config.algorithm_id != "tracking" or config.options.get("invalid"):
            raise MindMapError("跟踪布局配置无效")

    def layout(self, document, measurements=None, *, expand_all=False, focus_id=None):
        self.calls.append((deepcopy(document.layout), expand_all, focus_id))
        copy = deepcopy(document)
        copy.layout = LayoutConfig()
        return TreeLayout().layout(copy, measurements, expand_all=expand_all, focus_id=focus_id)


def test_widget_export_shares_registry_and_preserves_state(qtbot, tmp_path):
    registry = create_default_layout_registry()
    tracker = TrackingLayout()
    registry.register(tracker)
    engine = MindMapEngine()
    engine.add_child(engine.root_id, "子节点")
    widget = MindMapWidget(engine, layout_registry=registry)
    qtbot.addWidget(widget)
    widget.set_layout(LayoutConfig(algorithm_id="tracking"))
    before = engine.document
    revision = engine.revision
    count = len(tracker.calls)
    widget.export(tmp_path / "注入布局.svg", expand_all=True)
    assert len(tracker.calls) == count + 1 and tracker.calls[-1][1]
    assert widget.layout_registry is registry
    assert engine.document == before and engine.revision == revision
    for config in (
        LayoutConfig(algorithm_id="missing"),
        LayoutConfig(algorithm_id="tracking", options={"invalid": True}),
    ):
        with pytest.raises(MindMapError):
            widget.set_layout(config)
        assert engine.document == before and engine.revision == revision
    widget.dispose()


def test_clipboard_uses_injected_codec_and_rejects_old_version(qtbot, monkeypatch):
    class TrackingJson(JsonCodec):
        def __init__(self):
            self.reads = self.writes = 0

        def read(self, stream, *, options):
            self.reads += 1
            return super().read(stream, options=options)

        def write(self, document, stream, *, options):
            self.writes += 1
            super().write(document, stream, options=options)

    registry = create_default_format_registry()
    # 使用一个独立注册表装配自定义 JSON，不修改任何模块级默认实例。
    from mindmap_engine import FormatRegistry

    injected = FormatRegistry()
    codec = TrackingJson()
    for entry in registry.list():
        injected.register(
            entry.info,
            importer=codec if entry.info.id == "json" else entry.importer,
            exporter=codec if entry.info.id == "json" else entry.exporter,
        )
    engine = MindMapEngine()
    child = engine.add_child(engine.root_id, "复制节点")
    widget = MindMapWidget(engine, format_registry=injected)
    qtbot.addWidget(widget)
    clipboard = QApplication.clipboard()
    original = bytes(clipboard.mimeData().data(CLIPBOARD_MIME)) if clipboard.mimeData() else b""
    try:
        widget.select_nodes([child])
        widget.copy()
        assert codec.writes == 1
        widget.select_nodes([engine.root_id])
        assert widget.paste() and codec.reads == 1
        before, revision = engine.document, engine.revision
        selection = widget.selected_node_ids()
        mime = QMimeData()
        mime.setData(CLIPBOARD_MIME, b'{"format":"mindmap","schema_version":1}')
        clipboard.setMimeData(mime)
        with pytest.raises(MindMapError, match="版本"):
            widget.paste()
        assert engine.document == before and engine.revision == revision
        assert widget.selected_node_ids() == selection
    finally:
        mime = QMimeData()
        if original:
            mime.setData(CLIPBOARD_MIME, original)
        clipboard.setMimeData(mime)
        widget.dispose()
