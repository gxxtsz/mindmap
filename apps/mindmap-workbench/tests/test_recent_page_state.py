"""本机页面访问记录的版本兼容、路径隔离、容量上限及写入失败恢复。"""

import json

import pytest
from mindmap_workbench.document_state import RECENT_PAGE_LIMIT, DocumentState, path_key


def test_page_records_roundtrip_deduplicate_limit_and_preserve_exit_snapshot(tmp_path):
    state = DocumentState(tmp_path / "记录.json")
    path = tmp_path / "导图.mindmap"
    exit_snapshot = [{"id": "mindmap." + "a" * 32, "path": str(path)}]
    state.save_session(exit_snapshot, exit_snapshot[0]["id"])
    original = state.store.path.read_bytes()
    keys = [(str(index), None) for index in range(RECENT_PAGE_LIMIT + 3)]
    assert state.set_recent_pages(path, [keys[0], *keys])
    assert state.pages_for(path / ".." / path.name) == tuple(keys[:RECENT_PAGE_LIMIT])
    assert state.store.path.read_bytes() == original
    state.flush_recent_pages()
    loaded = DocumentState(state.store.path)
    loaded.load()
    assert loaded.pages_for(path) == tuple(keys[:RECENT_PAGE_LIMIT])
    assert loaded.open_documents == exit_snapshot and loaded.active_id == exit_snapshot[0]["id"]
    assert not state.recent_pages_dirty
    assert not state.set_recent_pages(path, keys[:RECENT_PAGE_LIMIT])
    state.clear_recent()
    assert state.pages_for(path) == tuple(keys[:RECENT_PAGE_LIMIT])


@pytest.mark.parametrize("pages", [None, [], "错误", {"相对路径.mindmap": [{}]}])
def test_bad_optional_page_field_does_not_break_legacy_records(tmp_path, pages):
    state = DocumentState(tmp_path / "记录.json")
    payload = {"schema_version": 1, "recent_files": [], "open_documents": []}
    if pages is not None:
        payload["recent_pages"] = pages
    state.store.path.write_text(json.dumps(payload), encoding="utf-8")
    state.load()
    assert state.recent_pages == {} and state.open_documents == []


def test_loading_skips_only_bad_entries_and_never_invents_main_page(tmp_path):
    path = tmp_path / "导图.mindmap"
    state = DocumentState(tmp_path / "记录.json")
    state.store.path.write_text(json.dumps({
        "schema_version": 1, "recent_files": [str(path)], "open_documents": [],
        "recent_pages": {str(path): [None, {}, {"page_id": []}, {"page_id": ""},
                                     {"page_id": None, "inspiration_root_id": "错误"},
                                     {"page_id": "页面"}, {"page_id": "页面"},
                                     {"page_id": "@inspiration", "inspiration_root_id": "想法"}]},
    }), encoding="utf-8")
    state.load()
    assert state.recent_files == [str(path)]
    assert state.pages_for(path) == (("页面", None), ("@inspiration", "想法"))
    assert list(state.recent_pages) == [path_key(path)]


def test_page_write_failure_preserves_file_and_memory_for_retry(tmp_path, monkeypatch):
    state = DocumentState(tmp_path / "记录.json")
    path = tmp_path / "导图.mindmap"
    state.remember(path)
    original = state.store.path.read_bytes()
    state.set_recent_pages(path, [(None, None), ("页面", None)])
    with monkeypatch.context() as patch:
        def fail(*args):
            raise OSError("磁盘不可写")
        patch.setattr("mindmap_docking.storage.os.replace", fail)
        with pytest.raises(OSError, match="磁盘不可写"):
            state.flush_recent_pages()
    assert state.store.path.read_bytes() == original and state.recent_pages_dirty
    assert state.pages_for(path) == ((None, None), ("页面", None))
    state.flush_recent_pages()
    assert not state.recent_pages_dirty and state.store.path.read_bytes() != original
