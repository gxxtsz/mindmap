"""覆盖目录往返、文件粒度、版本冲突及进程中断后的恢复。"""

import os
import subprocess
import sys

import pytest
from mindmap_core import GraphNode, MindMapEngine, NodeGraph
from mindmap_core.formats import DirectoryVersionConflict, directory_version, load_directory, save_directory


@pytest.mark.parametrize("checkout_crlf", [False, True])
def test_roundtrip_and_one_detail_edit_changes_only_its_file(tmp_path, checkout_crlf):
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "子图")
    submap = engine.create_submap(owner)
    submap_root = engine.page_document(submap).root_id
    engine.set_node_graph(owner, NodeGraph((GraphNode("graph", "图节点"),)))
    detail = engine.create_graph_node_page(owner, "graph", kind="detail")
    detail_root = engine.page_document(detail).root_id
    first = engine.add_child(detail_root, "正文")
    engine.add_child(detail_root, "后续内容")
    image = "同一张图片".encode("utf-8")
    engine.add_image_child(detail_root, image, width=32)
    engine.add_image_child(submap_root, image, width=64)
    reference = engine.add_child(engine.root_id, "引用")
    engine.set_page_reference(reference, submap_root)
    path = tmp_path / "第二大脑"
    version = save_directory(engine.document, path, expected_version=None)
    assert load_directory(path) == engine.document
    assert len(list((path / "assets").iterdir())) == 1
    if checkout_crlf:
        for name, _, _ in version.files:
            if name.endswith(".json"):
                member = path / name
                member.write_bytes(member.read_bytes().replace(b"\n", b"\r\n"))
        version = directory_version(path)

    original_files = {name: (path / name).stat().st_mtime_ns for name, _, _ in version.files}
    repo_metadata = path / "README.md"
    repo_metadata.write_text("本地知识库", encoding="utf-8")
    engine.update_node(first, text="更新详情页正文")
    updated = save_directory(engine.document, path, expected_version=version)
    before = {name: digest for name, _, digest in version.files}
    after = {name: digest for name, _, digest in updated.files}
    changed_file = f"pages/{detail}.json"
    assert before.keys() == after.keys()
    assert [name for name in after if after[name] != before[name]] == [changed_file]
    assert all((path / name).stat().st_mtime_ns == stamp
               for name, stamp in original_files.items() if name != changed_file)
    assert repo_metadata.read_text(encoding="utf-8") == "本地知识库"
    assert load_directory(path) == engine.document
    assert directory_version(path) == updated


def test_page_change_detected_even_with_size_and_timestamp_preserved(tmp_path):
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "详情入口")
    page = engine.create_detail_page(owner)
    child = engine.add_child(engine.page_document(page).root_id, "原文件")
    path = tmp_path / "导图"
    version = save_directory(engine.document, path)
    member = path / "pages" / f"{page}.json"
    info = member.stat()
    external = member.read_bytes().replace("原文件".encode(), "外部改".encode())
    member.write_bytes(external)
    os.utime(member, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert member.stat().st_size == info.st_size

    engine.update_node(child, text="本地修改")
    with pytest.raises(DirectoryVersionConflict, match="外部修改"):
        save_directory(engine.document, path, expected_version=version)
    assert member.read_bytes() == external
    assert load_directory(path).nodes[child].text == "外部改"


@pytest.mark.parametrize("interruption", ["写入异常", "进程退出"])
def test_interrupted_multi_file_save_recovers_previous_snapshot(tmp_path, interruption):
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "旧详情")
    old_page = engine.create_detail_page(owner)
    path = tmp_path / "导图"
    original = engine.document
    version = save_directory(original, path)
    script = """
import os
import sys
from pathlib import Path
from mindmap_core import MindMapEngine, MindMapError
from mindmap_core.formats import load_directory, save_directory
from mindmap_core.formats import directory_storage

path = Path(sys.argv[1])
engine = MindMapEngine(load_directory(path))
engine.remove_detail_page(engine.document.root.children[0])
owner = engine.add_child(engine.root_id, "新详情")
engine.create_detail_page(owner)
replace = directory_storage.os.replace

def fail_before_manifest(source, target):
    if target == path / "mindmap.json":
        if sys.argv[2] == "进程退出":
            os._exit(43)
        raise OSError("模拟写入失败")
    return replace(source, target)

directory_storage.os.replace = fail_before_manifest
try:
    save_directory(engine.document, path)
except MindMapError as error:
    assert "模拟写入失败" in str(error)
    sys.exit(44)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path), interruption],
        capture_output=True, timeout=15,
    )
    assert result.returncode == (43 if interruption == "进程退出" else 44), result.stderr
    if interruption == "进程退出":
        assert not (path / "pages" / f"{old_page}.json").exists()
        assert list(tmp_path.glob(".mindmap-save-*"))
    assert load_directory(path) == original
    assert directory_version(path) == version
    assert not list(tmp_path.glob(".mindmap-save-*"))
