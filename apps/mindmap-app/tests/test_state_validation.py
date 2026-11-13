"""恢复标识损坏时安全降级，保证不会阻断启动或使保存去重误跳过文档。"""

import json

from mindmap_app.migration import migrate_legacy
from mindmap_desktop_runtime.state import StateStore


def test_legacy_malformed_identifiers_do_not_discard_valid_records(tmp_path):
    path = str(tmp_path / "历史导图.mindmap")
    valid = {"id": "mindmap.valid", "path": path, "view_state": {"page_id": "子导图"}}
    legacy = tmp_path / "documents-v1.json"
    raw = json.dumps({"schema_version": 1, "active_id": [], "recent_files": [path],
                      "open_documents": [{"id": "", "path": path}, {**valid, "document_id": []}]}).encode()
    legacy.write_bytes(raw)
    converted, _settings = migrate_legacy(tmp_path)
    state = StateStore(tmp_path, converted)
    assert state.open_documents == [valid]
    assert state.active_id == "" and state.recent_files == [path]
    assert legacy.read_bytes() == raw


def test_malformed_legacy_navigation_does_not_interrupt_conversion(tmp_path):
    """历史列表和布局索引局部损坏时跳过无效条目，仍以新状态启动且保留来源。"""
    legacy = tmp_path / "documents-v1.json"
    raw = json.dumps({
        "schema_version": 1, "open_documents": None, "recent_files": None,
        "layout": {"schema_version": 1, "session": {"application_id": [], "active": [],
            "editor": {"kind": "tabs", "tokens": [None, [], "tool:software-structure.module-api"], "current": "错误"}}},
    }).encode()
    legacy.write_bytes(raw)
    converted, _settings = migrate_legacy(tmp_path)
    state = StateStore(tmp_path, converted)
    assert not state.open_documents and not state.recent_files
    assert state.data["layout"]["session"]["editor"]["tokens"] == []
    assert state.data["layout"]["session"]["editor"]["current"] == 0
    assert legacy.read_bytes() == raw
