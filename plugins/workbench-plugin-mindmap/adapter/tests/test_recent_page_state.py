"""页面历史通过插件状态服务保存，兼容旧字段并与通用退出记录隔离。"""

import json

import pytest
from workbench_api import path_key
from workbench_plugin_mindmap.page_history import RECENT_PAGE_LIMIT, PageHistoryState
from workbench_runtime.state import StateStore


def state_pair(tmp_path):
    """使用真实原子存储；业务对象只获得命名空间服务，不接触文件路径。"""
    owner = StateStore(tmp_path)
    owner.restoring = False
    state = PageHistoryState(owner.plugin_state("mindmap"))
    state.load()
    return owner, state


def test_page_records_roundtrip_deduplicate_limit_and_preserve_exit_snapshot(tmp_path):
    owner, state = state_pair(tmp_path)
    path = tmp_path / "导图.mindmap"
    exit_snapshot = [{"id": "mindmap.saved", "path": str(path), "plugin_id": "mindmap", "editor_type": "mindmap.editor"}]
    owner.save_session(exit_snapshot, exit_snapshot[0]["id"])
    original = owner.store.path.read_bytes()
    keys = [(str(index), None) for index in range(RECENT_PAGE_LIMIT + 3)]
    assert state.set_recent_pages(path, [keys[0], *keys])
    assert state.pages_for(path / ".." / path.name) == tuple(keys[:RECENT_PAGE_LIMIT])
    assert owner.store.path.read_bytes() == original
    state.flush()
    loaded_owner, loaded = state_pair(tmp_path)
    assert loaded.pages_for(path) == tuple(keys[:RECENT_PAGE_LIMIT])
    assert loaded_owner.open_documents == exit_snapshot and loaded_owner.active_id == exit_snapshot[0]["id"]
    assert not state.dirty and not state.set_recent_pages(path, keys[:RECENT_PAGE_LIMIT])
    owner.clear_recent()
    assert state.pages_for(path) == tuple(keys[:RECENT_PAGE_LIMIT])
    payload = json.loads(owner.store.path.read_text("utf-8"))["plugins"]["mindmap"]["page_history"]
    assert set(payload) == {"schema_version", "recent_pages"}


@pytest.mark.parametrize("pages", [None, [], "错误", {"相对路径.mindmap": [{}]}])
def test_bad_optional_page_field_does_not_break_legacy_records(tmp_path, pages):
    owner, state = state_pair(tmp_path)
    payload = {"schema_version": 1, "recent_files": [], "open_documents": [], "active_id": ""}
    if pages is not None:
        payload["recent_pages"] = pages
    owner.plugin_state("mindmap").set("page_history", payload)
    state.load()
    assert state.pages == {} and owner.open_documents == []


def test_loading_skips_only_bad_entries_and_never_invents_main_page(tmp_path):
    path = tmp_path / "导图.mindmap"
    owner, state = state_pair(tmp_path)
    owner.remember(path)
    owner.plugin_state("mindmap").set("page_history", {
        "schema_version": 1, "recent_files": [str(path)], "open_documents": [],
        "recent_pages": {str(path): [None, {}, {"page_id": []}, {"page_id": ""},
                                     {"page_id": None, "inspiration_root_id": "错误"},
                                     {"page_id": "页面"}, {"page_id": "页面"},
                                     {"page_id": "@inspiration", "inspiration_root_id": "想法"}]},
    })
    state.load()
    assert owner.recent_files == [str(path)]
    assert state.pages_for(path) == (("页面", None), ("@inspiration", "想法"))
    assert list(state.pages) == [path_key(path)]


def test_page_write_failure_preserves_file_and_memory_for_retry(tmp_path, monkeypatch):
    owner, state = state_pair(tmp_path)
    path = tmp_path / "导图.mindmap"
    owner.remember(path)
    original = owner.store.path.read_bytes()
    state.set_recent_pages(path, [(None, None), ("页面", None)])
    with monkeypatch.context() as patch:
        def fail(*args):
            raise OSError("磁盘不可写")
        patch.setattr("workbench_docking.storage.os.replace", fail)
        with pytest.raises(OSError, match="磁盘不可写"):
            state.flush()
    assert owner.store.path.read_bytes() == original and state.dirty
    assert state.pages_for(path) == ((None, None), ("页面", None))
    state.flush()
    assert not state.dirty and owner.store.path.read_bytes() != original
