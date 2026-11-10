"""通用历史与退出快照的容量、隔离、所有权和原子提交验收。"""

import json
import sys
from copy import deepcopy

import pytest
from workbench_api import path_key
from workbench_runtime.state import StateStore


def record(tmp_path):
    return {"id": "view.saved", "path": str(tmp_path / "退出文档.example"),
            "plugin_id": "sample", "editor_type": "sample.editor"}


def writable_state(tmp_path):
    state = StateStore(tmp_path)
    state.restoring = False
    return state


def test_recent_limit_deduplication_preserves_exit_snapshot(tmp_path):
    state = writable_state(tmp_path)
    first = record(tmp_path)
    state.save_session([first], first["id"])
    paths = [tmp_path / f"最近 {index}.txt" for index in range(23)]
    for path in paths:
        state.remember(path)
    assert state.recent_files == [str(path) for path in reversed(paths[-20:])]
    state.remember(paths[-2].parent / "." / paths[-2].name)
    assert state.recent_files[0] == str(paths[-2]) and len(state.recent_files) == 20
    restored = writable_state(tmp_path)
    assert restored.recent_files == state.recent_files
    assert restored.open_documents == [first] and restored.active_id == first["id"]
    restored.clear_recent()
    payload = json.loads(state.store.path.read_text("utf-8"))
    assert payload["recent_files"] == [] and payload["views"] == [first]
    assert payload["active_id"] == first["id"]


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 使用不区分大小写的路径键")
def test_windows_history_deduplicates_path_case(tmp_path):
    state = writable_state(tmp_path)
    source = tmp_path / "Case.txt"
    state.remember(source)
    state.remember(str(source).upper())
    assert len(state.recent_files) == 1
    assert path_key(state.recent_files[0]) == path_key(source)


def test_atomic_write_failure_preserves_file_and_cleans_temporary_file(tmp_path, monkeypatch):
    state = writable_state(tmp_path)
    state.remember(tmp_path / "甲.txt")
    original = state.store.path.read_bytes()

    def fail(*args):
        raise OSError("模拟原子替换失败")

    monkeypatch.setattr("workbench_runtime.storage.os.replace", fail)
    with pytest.raises(OSError, match="替换失败"):
        state.remember(tmp_path / "乙.txt")
    assert state.store.path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [state.store.path]


@pytest.mark.parametrize("invalid", [
    {"schema_version": 2, "recent_files": [], "views": []},
    {"schema_version": 1, "recent_files": "错误类型", "views": []},
    {"schema_version": 1, "recent_files": ["relative.txt"], "views": []},
    {"schema_version": 1, "recent_files": [], "views": [{"id": "view.invalid", "path": "错误"}]},
])
def test_invalid_state_backs_up_without_installing_partial_records(tmp_path, invalid):
    state = StateStore(tmp_path)
    raw = json.dumps(invalid).encode("utf-8")
    state.store.path.write_bytes(raw)
    restored = StateStore(tmp_path)
    assert restored.error
    assert not restored.recent_files and not restored.open_documents and not restored.active_id
    assert next(tmp_path.glob("workbench-state-invalid-*.json")).read_bytes() == raw


def test_nested_snapshot_is_owned_and_failed_commit_rolls_back(tmp_path, monkeypatch):
    state = writable_state(tmp_path)
    view = {**record(tmp_path), "view_state": {"version": 1, "center": [10, 20], "selection": ["节点"]}}
    expected = deepcopy(view)
    state.save_session([view], view["id"])
    saved = state.store.path.read_bytes()
    view["view_state"]["center"].clear()
    view["view_state"]["selection"].append("之后的修改")
    assert state.open_documents == StateStore(tmp_path).open_documents == [expected]

    def fail(payload):
        raise OSError("模拟配置写入失败")

    monkeypatch.setattr(state.store, "save", fail)
    with pytest.raises(OSError, match="配置写入失败"):
        state.save_session([], "")
    assert state.open_documents == [expected] and state.active_id == expected["id"]
    assert state.store.path.read_bytes() == saved
