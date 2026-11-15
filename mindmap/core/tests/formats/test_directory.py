"""覆盖目录往返、文件粒度、版本冲突及进程中断后的恢复。"""

import os
import subprocess
import sys

import pytest
from mindmap_core import GraphNode, MindMapEngine, NodeGraph
from mindmap_core.formats import (
    DirectoryVersionConflict,
    directory,
    directory_version,
    load_directory,
    prepare_directory_update,
    save_directory,
)


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("checkout_crlf", [False, True])
def test_roundtrip_and_one_detail_edit_changes_only_its_file(tmp_path, monkeypatch, checkout_crlf, incremental):
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
    saved = engine.capture_save_snapshot()
    version = save_directory(saved.document, path, expected_version=None)
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
    snapshot = engine.capture_save_snapshot()
    if incremental:
        update = prepare_directory_update(snapshot, saved, version)
        assert update.manifest is None and not update.assets and not update.removed
        encoded = []
        encode_page = directory._encode_page

        def track(page, nodes):
            encoded.append(page.id if page is not None else None)
            return encode_page(page, nodes)

        monkeypatch.setattr(directory, "_encode_page", track)
        updated = save_directory(update, path)
        assert encoded == [detail]
    else:
        updated = save_directory(snapshot.document, path, expected_version=version)
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


@pytest.mark.parametrize("incremental", [False, True])
def test_page_change_detected_even_with_size_and_timestamp_preserved(tmp_path, incremental):
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "详情入口")
    page = engine.create_detail_page(owner)
    child = engine.add_child(engine.page_document(page).root_id, "原文件")
    path = tmp_path / "导图"
    saved = engine.capture_save_snapshot()
    version = save_directory(saved.document, path)
    member = path / "pages" / f"{page}.json"
    info = member.stat()
    external = member.read_bytes().replace("原文件".encode(), "外部改".encode())
    member.write_bytes(external)
    os.utime(member, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert member.stat().st_size == info.st_size

    engine.update_node(engine.root_id, text="本地修改")
    snapshot = engine.capture_save_snapshot()
    payload = prepare_directory_update(snapshot, saved, version) if incremental else snapshot.document
    with pytest.raises(DirectoryVersionConflict, match="外部修改"):
        save_directory(payload, path, expected_version=version)
    assert member.read_bytes() == external
    assert load_directory(path).nodes[child].text == "外部改"


def test_incremental_save_tracks_page_ownership_and_restores_removed_files(tmp_path):
    """图节点随所属树页保存，跨页移动、级联删除和撤销更新各自的目录成员。"""
    engine = MindMapEngine()
    branch = engine.add_child(engine.root_id, "图入口")
    engine.add_child(branch, "一起移动的后代")
    engine.set_node_graph(branch, NodeGraph((GraphNode("graph", "图节点"),)))
    detail = engine.create_graph_node_page(branch, "graph", kind="detail")
    root = engine.page_document(detail).root_id
    image = engine.attach_image(root, b"image")
    main_file, inspiration_file = "pages/main.json", "pages/inspiration.json"
    detail_file, asset_file = f"pages/{detail}.json", f"assets/{image}"
    path = tmp_path / "导图"
    saved = engine.capture_save_snapshot()
    version = save_directory(saved.document, path, expected_version=None)

    def commit(pages, *, manifest=False, removed=(), assets=()):
        nonlocal saved, version
        snapshot = engine.capture_save_snapshot()
        update = prepare_directory_update(snapshot, saved, version)
        assert set(update.pages) == set(pages)
        assert (update.manifest is not None) == manifest
        assert update.removed == set(removed)
        assert set(update.assets) == set(assets)
        version = save_directory(update, path)
        assert load_directory(path) == snapshot.document
        assert directory_version(path) == version
        saved = snapshot

    # 详情页根的标题同时保存在图节点及其所属主图。
    engine.update_node(root, text="共享标题")
    commit({main_file, detail_file})

    engine.move_to_inspiration([branch], positions={branch: (10, 20)})
    commit({main_file, inspiration_file})

    engine.remove_nodes([branch])
    commit({inspiration_file}, manifest=True, removed={detail_file, asset_file})

    assert engine.undo()
    commit({inspiration_file, detail_file}, manifest=True, assets={asset_file})

    assert engine.redo()
    commit({inspiration_file}, manifest=True, removed={detail_file, asset_file})


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
from mindmap_core.formats import directory_version, load_directory, prepare_directory_update, save_directory
from mindmap_core.formats import directory_storage

path = Path(sys.argv[1])
engine = MindMapEngine(load_directory(path))
saved = engine.capture_save_snapshot()
version = directory_version(path)
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
    save_directory(prepare_directory_update(engine.capture_save_snapshot(), saved, version), path)
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
