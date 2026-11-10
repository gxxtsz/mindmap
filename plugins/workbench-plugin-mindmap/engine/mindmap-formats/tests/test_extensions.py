"""格式扩展、版本边界和未知布局的独立持久化。"""

from copy import deepcopy
from io import BytesIO

import pytest
from mindmap_core import FormatInfo, LayoutConfig, MindMapDocument, MindMapError
from mindmap_formats import (
    create_default_format_registry,
    decode_document,
    encode_document,
    from_dict,
    load_document,
    loads,
    save_document,
    to_dict,
)


@pytest.mark.parametrize("format_id,suffix", [("mindmap", ".mindmap"), ("json", ".json")])
def test_unknown_layout_roundtrip_without_implementation(tmp_path, format_id, suffix):
    document = MindMapDocument.new("未知布局仍可交换")
    document.layout = LayoutConfig(algorithm_id="external.layout", options={"中文参数": [4, {"尺寸": 16}]})
    before = deepcopy(document)
    target = tmp_path / ("导图" + suffix)
    save_document(document, target, format_id=format_id)
    assert load_document(target) == before
    assert decode_document(encode_document(document, format_id=format_id), format_id=format_id) == before
    assert document == before


@pytest.mark.parametrize("change", ["version", "missing-layout", "old-direction", "bad-options", "assets"])
def test_only_current_complete_schema_is_accepted(change):
    data = to_dict(MindMapDocument.new())
    if change == "version":
        data["schema_version"] = 1
    elif change == "missing-layout":
        del data["layout"]
    elif change == "old-direction":
        data["direction"] = "left"
    elif change == "bad-options":
        del data["layout"]["options"]
    else:
        data["assets"] = {}
    for value, parser in ((data, from_dict), (__import__("json").dumps(data), loads)):
        with pytest.raises(MindMapError):
            parser(value)


def test_custom_export_atomic_failure_and_explicit_dispatch(tmp_path):
    class CustomExporter:
        def write(self, document, stream, *, options):
            stream.write(b"partial")
            raise RuntimeError("测试扩展失败")

    registry = create_default_format_registry()
    registry.register(FormatInfo("failing", "失败导出器", (".failed",)), exporter=CustomExporter())
    target = tmp_path / "已有文件.failed"
    target.write_bytes(b"original")
    document = MindMapDocument.new()
    with pytest.raises(MindMapError, match="导出文档失败"):
        save_document(document, target, registry=registry)
    assert target.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [target]
    assert not create_default_format_registry().get("markdown").can_save
    assert create_default_format_registry().get("json").can_save
    # 显式格式标识优先于文件扩展名，允许宿主通过文件选择器准确选择实现。
    target = tmp_path / "任意名字.bin"
    save_document(document, target, registry=registry, format_id="json")
    assert load_document(target, registry=registry, format_id="json") == document


def test_json_stream_uses_same_duplicate_field_check():
    registry = create_default_format_registry()
    content = b'{"format":"mindmap","format":"other"}'
    with pytest.raises(MindMapError, match="重复"):
        registry.read(BytesIO(content), format_id="json")


def test_schema_layout_does_not_share_nested_options():
    data = to_dict(MindMapDocument.new())
    data["layout"]["options"] = {"nested": [1, 2]}
    document = from_dict(data)
    data["layout"]["options"]["nested"].append(3)
    assert document.layout.options == {"nested": [1, 2]}
