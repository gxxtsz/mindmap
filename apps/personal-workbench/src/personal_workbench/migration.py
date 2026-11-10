"""产品历史配置的只读导入及启动时升级。"""

from copy import deepcopy
from os.path import normpath
from pathlib import Path

from PySide6.QtCore import QSettings
from workbench_runtime.storage import JsonLayoutStore


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


def upgrade_profile(state: dict, settings: dict) -> tuple[bool, bool]:
    """清理已退役插件的记录，分别返回状态和设置的变更标记。

    状态和设置由宿主校验后传入，本函数只修改这两份字典。最近文件的旧格式仅保存
    路径，所以首次升级按扩展名判断，并保留已关联其他插件视图的路径。迁移标记随
    状态保存，后续安装的插件可继续把这些扩展名的文件加入最近列表。
    """
    state_changed = state["plugins"].pop("text", None) is not None
    removed_ids = {view["id"] for view in state["views"] if view["plugin_id"] == "text"}
    if removed_ids:
        state["views"] = [view for view in state["views"] if view["id"] not in removed_ids]
        state_changed = True
    # 旧版也会记录未保存文本的活动标签标识，即使对应视图未写入会话。
    active_id = state["active_id"]
    unsaved_text = active_id.startswith("text.") and all(view["id"] != active_id for view in state["views"])
    if active_id in removed_ids or unsaved_text:
        state["active_id"] = ""
        state_changed = True
    if "product_profile_revision" not in state:
        kept_paths = {Path(normpath(view["path"])) for view in state["views"]}
        state["recent_files"] = [path for path in state["recent_files"]
                                 if Path(path).suffix.lower() not in {".txt", ".md"}
                                 or Path(normpath(path)) in kept_paths]
        state["product_profile_revision"] = 1
        state_changed = True

    settings_changed = settings.pop("text", None) is not None
    plugin_settings = settings.get("plugins", {})
    if "enabled/text" in plugin_settings:
        del plugin_settings["enabled/text"]
        settings_changed = True
    return state_changed, settings_changed
