"""恢复标识损坏时安全降级，保证不会阻断启动或使保存去重误跳过文档。"""

import json

from personal_workbench.migration import migrate_legacy
from workbench_runtime.state import StateStore


def test_legacy_malformed_identifiers_do_not_discard_valid_records(tmp_path):
    path = str(tmp_path / "历史导图.mindmap")
    valid = {"id": "mindmap.valid", "path": path, "view_state": {"page_id": "子导图"}}
    legacy = tmp_path / "documents-v1.json"
    raw = json.dumps({"schema_version": 1, "active_id": [], "recent_files": [path],
                      "open_documents": [{"id": "", "path": path}, {**valid, "document_id": []}]}).encode()
    legacy.write_bytes(raw)
    converted, _settings = migrate_legacy(tmp_path)
    assert set(converted["plugins"]["mindmap"]["page_history"]) == {"schema_version", "recent_pages"}
    state = StateStore(tmp_path, converted)
    assert state.open_documents == [{**valid, "plugin_id": "mindmap", "editor_type": "mindmap.editor"}]
    assert state.active_id == "" and state.recent_files == [path]
    assert legacy.read_bytes() == raw
