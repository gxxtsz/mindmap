"""产品历史格式的只读转换；通用平台不解释导图专属记录和偏好。"""

from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QSettings
from workbench_docking import JsonLayoutStore


def migrate_legacy(directory):
    """仅在对应新文件尚不存在时读取旧文件；不移动、不修改任何历史配置。"""
    directory = Path(directory)
    state = {"schema_version": 1, "views": [], "recent_files": [], "active_id": "", "plugins": {}, "layout": None}
    settings = {}
    if not (directory / "workbench-state-v1.json").exists():
        try:
            old = JsonLayoutStore(directory / "documents-v1.json").load()
            if old and old.get("schema_version") == 1:
                seen = set()
                for record in old.get("open_documents", []):
                    if (not isinstance(record, dict) or not isinstance(record.get("id"), str) or
                            not record["id"] or
                            not isinstance(record.get("path"), str) or record["id"] in seen or
                            not Path(record["path"]).is_absolute() or Path(record["path"]).suffix.lower() != ".mindmap"):
                        continue
                    seen.add(record["id"])
                    # 只迁移旧格式明确支持的字段，避免未知字段污染通用文档标识。
                    converted = {"id": record["id"], "path": record["path"],
                                 "plugin_id": "mindmap", "editor_type": "mindmap.editor"}
                    if isinstance(record.get("view_state"), dict):
                        converted["view_state"] = deepcopy(record["view_state"])
                    state["views"].append(converted)
                active_id = old.get("active_id", "")
                state["active_id"] = active_id if isinstance(active_id, str) and active_id in seen else ""
                state["recent_files"] = [item for item in old.get("recent_files", [])
                                         if isinstance(item, str) and Path(item).is_absolute()][:20]
                state["plugins"]["mindmap"] = {"page_history": {
                    "schema_version": 1, "recent_pages": deepcopy(old.get("recent_pages", {})),
                }}
        except (OSError, ValueError, TypeError):
            # 坏历史记录不阻止首次使用；原文件保留供用户修复，绝不写回空对象。
            pass
        try:
            state["layout"] = JsonLayoutStore(directory / "layouts-v1.json").load()
        except (OSError, ValueError):
            pass
    if not (directory / "workbench-settings-v1.json").exists() and (directory / "preferences.ini").exists():
        old = QSettings(str(directory / "preferences.ini"), QSettings.Format.IniFormat)
        for key in old.allKeys():
            namespace = "" if key.startswith("window/") else "mindmap"
            settings.setdefault(namespace, {})[key] = old.value(key)
    return state, settings
