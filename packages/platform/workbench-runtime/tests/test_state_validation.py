"""平台恢复标识校验；损坏记录降级时保留原始字节。"""

import json

import pytest
from workbench_runtime.state import StateStore


def payload(tmp_path):
    return {"schema_version": 1, "active_id": "view.a", "views": [
        {"id": "view.a", "document_id": "document.a", "plugin_id": "text",
         "editor_type": "text.editor", "path": str(tmp_path / "正文.txt")},
    ], "recent_files": [], "plugins": {}}


def assert_recovery_preserves_bytes(tmp_path, data):
    """真实经过文件读取和备份边界；失败记录不得提前安装到恢复集合。"""
    path = tmp_path / "workbench-state-v1.json"
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    path.write_bytes(raw)
    restored = StateStore(tmp_path)
    assert restored.error and restored.active_id == "" and restored.open_documents == []
    assert path.read_bytes() == raw
    assert next(tmp_path.glob("workbench-state-invalid-*.json")).read_bytes() == raw


@pytest.mark.parametrize("invalid", [[], {}, None, 9])
def test_invalid_active_identifier_keeps_backup_and_allows_startup(tmp_path, invalid):
    data = payload(tmp_path)
    data["active_id"] = invalid
    assert_recovery_preserves_bytes(tmp_path, data)


@pytest.mark.parametrize("invalid", [[], {}, None, "", 9])
def test_invalid_document_identifier_does_not_reach_save_deduplication(tmp_path, invalid):
    data = payload(tmp_path)
    data["views"][0]["document_id"] = invalid
    assert_recovery_preserves_bytes(tmp_path, data)


@pytest.mark.parametrize("same_resource", [True, False])
def test_conflicting_document_resource_identity_keeps_original_state(tmp_path, same_resource):
    data = payload(tmp_path)
    second = {**data["views"][0], "id": "view.b"}
    if same_resource:
        second["document_id"] = "document.b"
    else:
        second["path"] = str(tmp_path / "另一正文.txt")
    data["views"].append(second)
    assert_recovery_preserves_bytes(tmp_path, data)
