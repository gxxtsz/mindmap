"""只依赖核心包的扩展契约与状态隔离测试。"""

from copy import deepcopy
from dataclasses import dataclass, field
from io import BytesIO

import pytest
from mindmap_core import (
    ChangeSet,
    FormatInfo,
    FormatRegistry,
    LayoutConfig,
    LayoutInfo,
    LayoutRegistry,
    LayoutResult,
    MindMapDocument,
    MindMapEngine,
    MindMapError,
    Rect,
)


class SampleLayout:
    """测试算法使用 options 决定根位置，不依赖任何内置布局包。"""

    info = LayoutInfo("sample", "测试布局", LayoutConfig(algorithm_id="sample", options={"x": 12}))

    def validate_config(self, config):
        if not isinstance(config.options.get("x"), int):
            raise MindMapError("测试布局需要整数 x")

    def layout(self, document, measurements=None, *, expand_all=False, focus_id=None):
        root = focus_id or document.root_id
        rect = Rect(document.layout.options["x"], 0, 80, 40)
        # 故意改写输入，验证注册表隔离外部扩展的副作用。
        document.root.text = "扩展内部修改"
        if measurements is not None:
            measurements.clear()
        return LayoutResult(nodes={root: rect}, bounds=rect.padded(40))


class TextImporter:
    def read(self, stream, *, options):
        return MindMapDocument.new(stream.read().decode("utf-8"))


class TextExporter:
    def write(self, document, stream, *, options):
        stream.write(document.root.text.encode("utf-8"))
        document.root.text = "导出器内部修改"


def test_registry_dispatch_isolation_and_metadata():
    registry = LayoutRegistry()
    registry.register(SampleLayout())
    info = registry.info("sample")
    info.default_config.options["x"] = 999
    document = MindMapDocument.new("原文档")
    document.layout = registry.info("sample").default_config
    original = deepcopy(document)
    sizes = {document.root_id: (80, 40)}
    assert registry.layout(document, sizes).nodes[document.root_id].x == 12
    assert document == original and sizes == {document.root_id: (80, 40)}
    assert registry.list()[0].name == "测试布局"
    with pytest.raises(MindMapError, match="重复"):
        registry.register(SampleLayout())
    with pytest.raises(MindMapError, match="未注册"):
        LayoutRegistry().layout(document)
    with pytest.raises(MindMapError, match="整数"):
        registry.validate_config(LayoutConfig(algorithm_id="sample"))
    with pytest.raises(MindMapError, match="紧凑"):
        registry.validate_config(LayoutConfig(algorithm_id="sample", compact=True, options={"x": 1}))


def test_invalid_geometry_rejected_before_rendering():
    class BadLayout(SampleLayout):
        def layout(self, document, measurements=None, **kwargs):
            return LayoutResult(nodes={document.root_id: Rect(float("nan"), 0, 80, 40)})

    registry = LayoutRegistry()
    registry.register(BadLayout())
    document = MindMapDocument.new()
    document.layout = SampleLayout.info.default_config
    with pytest.raises(MindMapError, match="矩形"):
        registry.layout(document)


def test_one_way_formats_resolution_and_failures():
    registry = FormatRegistry()
    registry.register(FormatInfo("read", "只读格式", (".sample",)), importer=TextImporter())
    registry.register(FormatInfo("write", "只写格式", (".sample",)), exporter=TextExporter())
    registry.register(FormatInfo("other", "另一个只写格式", (".sample",)), exporter=TextExporter())
    assert registry.resolve("中文.SAMPLE").info.id == "read"
    with pytest.raises(MindMapError, match="歧义"):
        registry.resolve("中文.sample", writing=True)
    assert registry.resolve("任意后缀.bin", format_id="write", writing=True).info.id == "write"
    with pytest.raises(MindMapError, match="不支持导出"):
        registry.resolve("", format_id="read", writing=True)
    with pytest.raises(MindMapError, match="不支持导入"):
        registry.resolve("", format_id="write")
    with pytest.raises(MindMapError, match="重复"):
        registry.register(FormatInfo("read", "重复", (".x",)), importer=TextImporter())
    with pytest.raises(MindMapError, match="未注册"):
        FormatRegistry().get("read")
    original = MindMapDocument.new("中文根节点")
    stream = BytesIO()
    registry.write(original, stream, format_id="write")
    assert original.root.text == "中文根节点"
    stream.seek(0)
    assert registry.read(stream, format_id="read").root.text == "中文根节点"
    assert not any(entry.can_save for entry in registry.list())


def test_plugin_exceptions_are_chinese_business_errors():
    class BrokenImporter:
        def read(self, stream, *, options):
            raise RuntimeError("failure")

    registry = FormatRegistry()
    registry.register(FormatInfo("bad", "测试错误", (".bad",)), importer=BrokenImporter())
    with pytest.raises(MindMapError, match="导入文档失败"):
        registry.read(BytesIO(), format_id="bad")


def test_invalid_plugin_defaults_do_not_leave_partial_registration():
    class BadDefault(SampleLayout):
        info = LayoutInfo("sample", "错误默认布局", LayoutConfig(algorithm_id="sample", compact=True))

    class BrokenValidator(SampleLayout):
        def validate_config(self, config):
            raise RuntimeError("测试校验器异常")

    registry = LayoutRegistry()
    with pytest.raises(MindMapError, match="紧凑"):
        registry.register(BadDefault())
    with pytest.raises(MindMapError, match="注册布局算法失败"):
        registry.register(BrokenValidator())
    assert registry.list() == ()


def test_configuration_history_fragment_and_new_document_field():
    engine = MindMapEngine()
    child = engine.add_child(engine.root_id, "子节点")
    engine.mark_saved()
    config = LayoutConfig(
        algorithm_id="not-installed",
        direction="both",
        compact=True,
        options={"plugin": {"spacing": [12, 24]}},
    )
    engine.set_layout(config)
    config.options["plugin"]["spacing"].append(99)
    expected = deepcopy(engine.document.layout)
    assert expected.options["plugin"]["spacing"] == [12, 24]
    fragment = engine.copy_fragment([child])
    assert fragment.layout == expected
    fragment.layout.options.clear()
    assert engine.document.layout == expected
    assert engine.undo() and not engine.is_dirty
    assert engine.redo() and engine.document.layout == expected

    @dataclass
    class ExtendedDocument(MindMapDocument):
        custom_field: dict = field(default_factory=dict)

    before = ExtendedDocument.new()
    after = deepcopy(before)
    after.custom_field["未来字段"] = [1, 2]
    delta = ChangeSet.between(before, after)
    assert "custom_field" in delta.settings
    delta.apply(after, forward=False)
    assert after == before


@pytest.mark.parametrize(
    "config",
    [
        LayoutConfig(algorithm_id=""),
        LayoutConfig(direction="bad"),
        LayoutConfig(compact=1),
        LayoutConfig(options={"bad": float("nan")}),
        LayoutConfig(options={1: "bad"}),
    ],
)
def test_invalid_config_is_atomic(config):
    engine = MindMapEngine()
    before = engine.document
    with pytest.raises(MindMapError):
        engine.set_layout(config)
    assert engine.document == before and not engine.can_undo
