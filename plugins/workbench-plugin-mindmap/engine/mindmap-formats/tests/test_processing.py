"""独立进程保存能力由格式包提供，扩展实现和扩展数据保留同步语义。"""

from copy import deepcopy

import pytest
from mindmap_core import FormatRegistry, MindMapDocument, MindMapError, Node, ValidatedDocument
from mindmap_formats import (
    MindmapCodec,
    can_process_snapshot,
    create_default_format_registry,
    load_validated_document,
    save_document,
    supports_owned_load,
    supports_process_save,
)


def test_process_capability_is_read_only_and_specific_to_native_format():
    registry = create_default_format_registry()
    document = MindMapDocument.new()
    original = deepcopy(document)
    assert supports_process_save(registry) and can_process_snapshot(document)
    assert not supports_process_save(registry, format_id="json")
    assert not supports_process_save(FormatRegistry())
    assert document == original


@pytest.mark.parametrize("changed", [
    "none", "registry_class", "registry_method", "codec_class", "codec_method", "registry_class_method",
    "codec_class_method",
    "validated_method", "validated_class_method",
])
def test_owned_load_requires_unmodified_native_reader(changed, monkeypatch):
    """读取扩展可能保留结果所有权，只有内置独占解析结果允许直接交给引擎。"""
    class CustomRegistry(FormatRegistry):
        pass

    class CustomCodec(MindmapCodec):
        pass

    registry = create_default_format_registry()
    if changed in {"registry_class", "codec_class"}:
        info = registry.get("mindmap").info
        registry = CustomRegistry() if changed == "registry_class" else FormatRegistry()
        registry.register(info, importer=CustomCodec() if changed == "codec_class" else MindmapCodec())
    if changed == "registry_method":
        monkeypatch.setattr(registry, "read", lambda *args, **kwargs: None)
    if changed == "codec_method":
        monkeypatch.setattr(registry.get("mindmap").importer, "read", lambda *args, **kwargs: None)
    if changed == "registry_class_method":
        monkeypatch.setattr(FormatRegistry, "read", lambda *args, **kwargs: None)
    if changed == "codec_class_method":
        monkeypatch.setattr(MindmapCodec, "read", lambda *args, **kwargs: None)
    if changed == "validated_method":
        monkeypatch.setattr(registry.get("mindmap").importer, "read_validated", lambda *args, **kwargs: None)
    if changed == "validated_class_method":
        monkeypatch.setattr(MindmapCodec, "read_validated", lambda *args, **kwargs: None)
    assert supports_owned_load(registry) == (changed == "none")
    assert not supports_owned_load(FormatRegistry())


def test_validated_native_load_checks_once_and_returns_new_owned_document(tmp_path, monkeypatch):
    document = MindMapDocument.new("一次校验")
    source = tmp_path / "原生凭据.mindmap"
    save_document(document, source)
    original, calls = MindMapDocument.validate, []

    def counted(candidate):
        calls.append(candidate)
        return original(candidate)

    monkeypatch.setattr(MindMapDocument, "validate", counted)
    validation = load_validated_document(source)
    assert type(validation) is ValidatedDocument
    assert validation.document == document and validation.document is not document
    assert calls == [validation.document]
    assert validation.document.root is not document.root
    assert validation.document.root.children is not document.root.children


def test_validated_load_rejects_extension_before_reading_file(tmp_path):
    class CustomRegistry(FormatRegistry):
        pass

    with pytest.raises(MindMapError, match="普通加载入口"):
        load_validated_document(tmp_path / "不存在.mindmap", registry=CustomRegistry())


@pytest.mark.parametrize("changed", [
    "registry_class", "registry_method", "codec_class", "codec_method", "registry_class_method", "codec_class_method",
])
def test_custom_dispatch_or_codec_requires_synchronous_save(changed, monkeypatch):
    class CustomRegistry(FormatRegistry):
        def write(self, *args, **kwargs):
            return super().write(*args, **kwargs)

    class CustomCodec(MindmapCodec):
        pass

    default = create_default_format_registry()
    registry = CustomRegistry() if changed == "registry_class" else default
    if changed in {"registry_class", "codec_class"}:
        registry = registry if changed == "registry_class" else FormatRegistry()
        entry = default.get("mindmap")
        codec = CustomCodec() if changed == "codec_class" else MindmapCodec()
        registry.register(entry.info, importer=codec, exporter=codec)
    if changed == "registry_method":
        monkeypatch.setattr(registry, "write", lambda *args, **kwargs: None)
    if changed == "codec_method":
        monkeypatch.setattr(registry.get("mindmap").exporter, "write", lambda *args, **kwargs: None)
    if changed == "registry_class_method":
        monkeypatch.setattr(FormatRegistry, "write", lambda *args, **kwargs: None)
    if changed == "codec_class_method":
        monkeypatch.setattr(MindmapCodec, "write", lambda *args, **kwargs: None)
    assert not supports_process_save(registry)


@pytest.mark.parametrize("changed", ["extra_state", "node_subclass", "document_subclass"])
def test_extension_payload_is_not_silently_sent_to_another_process(changed):
    class CustomNode(Node):
        pass

    class CustomDocument(MindMapDocument):
        pass

    document = CustomDocument.new() if changed == "document_subclass" else MindMapDocument.new()
    if changed == "extra_state":
        document.root.custom_state = object()
    if changed == "node_subclass":
        root = CustomNode("扩展主题", id=document.root_id)
        document.nodes[root.id] = root
    assert not can_process_snapshot(document)
