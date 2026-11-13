"""格式往返与原子保存失败保护；不依赖布局实现或默认组合包。"""

import json
import zipfile

import pytest
from mindmap_core import BranchStyle, LayoutConfig, MindMapDocument, MindMapEngine, MindMapError
from mindmap_core.formats import (
    create_default_format_registry,
    dumps,
    encode_document,
    export_outline,
    from_dict,
    import_outline,
    load_document,
    load_validated_document,
    loads,
    save_document,
    to_dict,
)
from mindmap_core.formats.files import FileVersionConflict, file_version


def example():
    engine = MindMapEngine()
    with engine.transaction():
        engine.set_layout(LayoutConfig(direction="right"))
        a = engine.add_child(engine.root_id, "中文\n多行")
        b = engine.add_child(engine.root_id, "另一分支")
        engine.add_child(a, "内容")
        engine.attach_image(a, "测试图片字节".encode())
        engine.update_node(b, metadata={"用户字段": [1, "内容"]}, tags=["标签"])
    return engine.document


@pytest.mark.parametrize("suffix", [".mindmap", ".json"])
def test_roundtrip_assets_and_metadata(tmp_path, suffix):
    document = example()
    target = tmp_path / ("中文路径" + suffix)
    save_document(document, target)
    assert load_document(target) == document
    assert loads(dumps(document)) == document
    if suffix == ".json":
        stored = json.loads(target.read_text(encoding="utf-8"))
    else:
        with zipfile.ZipFile(target) as archive:
            stored = json.loads(archive.read("document.json"))
    assert stored["schema_version"] == 5
    assert "relations" not in stored and "summaries" not in stored


@pytest.mark.parametrize("kind", [None, "round", "line", "arrow"])
def test_loading_saved_documents_preserves_original_branch_style(tmp_path, kind):
    """新建默认值不回填已保存文档，包括未配置分支的旧曲线。"""
    engine = MindMapEngine()
    child = engine.add_child(engine.root_id)
    engine.set_branch_style([engine.root_id], kind)
    path = tmp_path / "保留原连接线.mindmap"
    save_document(engine.document, path)
    loaded = load_document(path)
    assert loaded == engine.document
    for node in (loaded.root, loaded.nodes[child]):
        assert node.style.get("branch") == (None if kind is None else {"kind": kind})


def test_imported_outline_uses_round_connections_at_every_depth():
    """无外观信息的大纲导入按新导图默认值初始化所有层级，节点配置互相独立。"""
    document = import_outline("根主题\n  一级\n    二级", markdown=False)
    assert all(node.style["branch"] == {"kind": "round"} for node in document.nodes.values())
    child = document.root.children[0]
    document.nodes[child].style["branch"]["kind"] = "line"
    assert document.root.style["branch"] == {"kind": "round"}


@pytest.mark.parametrize("kind", list(BranchStyle))
@pytest.mark.parametrize("suffix", [".mindmap", ".json"])
def test_branch_styles_keep_current_version_and_roundtrip(tmp_path, kind, suffix):
    """分支样式在当前版本原生包及 JSON 中完整保留。"""
    engine = MindMapEngine(example())
    engine.set_branch_style([engine.root_id], kind)
    first = engine.document.root.children[0]
    engine.set_branch_style([first], None, scope="level")
    document = engine.document
    target = tmp_path / ("分支样式" + suffix)
    save_document(document, target)
    loaded = load_document(target)
    assert loaded == document
    assert "branch" not in loaded.nodes[first].style
    assert loaded.root.style["branch"] == {"kind": kind.value}
    assert to_dict(loaded)["schema_version"] == 5


@pytest.mark.parametrize("branch", [{"kind": "未知"}, {}, None, {"kind": 8}])
def test_invalid_branch_configuration_rejected_on_load(branch):
    data = to_dict(example())
    data["nodes"][0]["style"]["branch"] = branch
    with pytest.raises(MindMapError, match="分支"):
        from_dict(data)


def test_atomic_save_failure_preserves_old_file(tmp_path, monkeypatch):
    target = tmp_path / "原文档.mindmap"
    target.write_bytes("原有文件".encode())

    def fail(*args):
        raise PermissionError("文件被占用")

    monkeypatch.setattr("mindmap_core.formats.files.os.replace", fail)
    with pytest.raises(MindMapError):
        save_document(example(), target)
    assert target.read_bytes() == "原有文件".encode()
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("originally_exists", [False, True])
def test_versioned_save_checks_target_after_encoding(tmp_path, monkeypatch, originally_exists):
    """编码期间外部创建或修改目标时，原子保存保留外部版本并清理临时文件。"""
    target = tmp_path / "目标.mindmap"
    if originally_exists:
        target.write_bytes(b"original")
    expected = file_version(target)
    registry = create_default_format_registry()
    write = registry.write

    def concurrent_write(*args, **kwargs):
        write(*args, **kwargs)
        target.write_bytes(b"external update")

    monkeypatch.setattr(registry, "write", concurrent_write)
    with pytest.raises(FileVersionConflict, match="外部修改"):
        save_document(example(), target, registry=registry, expected_version=expected)
    assert target.read_bytes() == b"external update"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("corruption", ["version", "duplicate", "reference", "resource", "cycle", "hash"])
def test_reject_corrupt_document(corruption, tmp_path):
    data = to_dict(example())
    if corruption == "version":
        data["schema_version"] = 999
    elif corruption == "duplicate":
        data["nodes"].append(data["nodes"][0])
    elif corruption == "reference":
        data["nodes"][0]["children"].append("缺失")
    elif corruption == "resource":
        data["assets"] = []
    elif corruption == "cycle":
        data["nodes"][1]["children"].append(data["root_id"])
    else:
        data["assets"][0]["data"] = "bm90LXRoZS1pbWFnZQ=="
    with pytest.raises(MindMapError):
        from_dict(data)
    path = tmp_path / "独占读取仍须拒绝损坏.mindmap"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("document.json", json.dumps(data))
    with pytest.raises(MindMapError):
        load_validated_document(path)


@pytest.mark.parametrize("text, key", [
    ('{"format":"mindmap","metadata":{"键":1,"键":1}}', "键"),
    ('{"format":"mindmap","metadata":{"键":1,"\\u952e":2}}', "键"),
    ('{"format":"mindmap","nodes":[{"id":"甲","id":"乙"}]}', "id"),
])
def test_duplicate_keys_are_rejected_in_nested_json_even_with_equal_values(text, key):
    with pytest.raises(MindMapError, match=f"JSON 字段重复：{key}"):
        loads(text)


def test_bad_zip_and_duplicate_json_keys(tmp_path):
    path = tmp_path / "损坏.mindmap"
    path.write_bytes("不是压缩文件".encode())
    with pytest.raises(MindMapError):
        load_document(path)
    with pytest.raises(MindMapError):
        loads('{"format":"mindmap","format":"其他"}')
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("document.json", json.dumps(to_dict(example(), embed_assets=False)))
        archive.writestr("../不允许的路径", "内容".encode())
    with pytest.raises(MindMapError):
        load_document(path)


@pytest.mark.parametrize("markdown", [True, False])
def test_outline_roundtrip_hierarchy(markdown):
    engine = MindMapEngine(MindMapDocument.new("根"))
    a = engine.add_child(engine.root_id, "甲")
    engine.add_child(a, "子项")
    engine.add_child(engine.root_id, "乙")
    outline = export_outline(engine.document, markdown=markdown)
    expected = "# 根\n- 甲\n  - 子项\n- 乙\n" if markdown else "根\n  甲\n    子项\n  乙\n"
    assert outline == expected
    imported = import_outline(outline, markdown=markdown)
    assert export_outline(imported, markdown=markdown) == outline


def test_tab_indented_text_export_writes_one_line_per_node():
    """验证 UTF-8 字节、根与同级顺序，以及多行正文和无正文节点的表示。"""
    engine = MindMapEngine()
    first = engine.add_inspiration("  甲根  ", position=(0, 0))
    branch = engine.add_child(first, "\t第一行\r\n第二行\t补充\u2028第三行 ")
    engine.update_node(branch, note="节点备注", tags=["节点标签"])
    engine.add_child(branch, "孙节点")
    engine.add_image_child(first, b"image", width=160)
    engine.add_child(first, " \t\n ")
    engine.add_inspiration("乙根", position=(200, 0))
    page = engine.page_document(engine.inspiration_id)

    data = encode_document(page, format_id="text", options={"tab_indent": True})

    expected = "甲根\n\t第一行 第二行 补充 第三行\n\t\t孙节点\n\t[图片]\n\t[空节点]\n乙根\n"
    assert data == expected.encode("utf-8")


def test_bad_outline():
    with pytest.raises(MindMapError):
        import_outline("# 根\n      - 跳级")
