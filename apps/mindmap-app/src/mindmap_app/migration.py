"""历史工作台配置的单向读取边界；旧名称及旧字段只在本模块解释。

新运行时只读取 mindmap-state-v2.json 和 mindmap-settings-v1.json。
首次启动时将历史现场转换为导图会话，后续保存新文件，始终保留原始配置字节。
"""

from copy import deepcopy
from pathlib import Path

from mindmap_desktop_runtime.storage import JsonLayoutStore
from PySide6.QtCore import QSettings


def _read_object(path: Path) -> dict | None:
    """只读取可解析的对象；损坏或暂不可读的历史文件留在原处，不覆盖为空状态。"""
    try:
        value = JsonLayoutStore(path).load()
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _source_directory(directory: Path) -> Path:
    """显式配置目录优先；默认新目录可读取原产品留下的子目录。"""
    names = ("workbench-state-v1.json", "documents-v1.json", "workbench-settings-v1.json", "preferences.ini")
    if any((directory / name).is_file() for name in names):
        return directory
    return directory / "mindmap-workbench"


def _convert_layout(layout, retired_ids: set[str]):
    """移除退役业务的标签和工具，保留导图分屏、尺寸、主题及窗口位置。"""
    def retired_tool(identifier):
        # 历史停靠标识使用连字符，Qt 对象名使用下划线；两种遗留记录均不再恢复。
        return isinstance(identifier, str) and identifier.startswith(("software-structure.", "software_structure"))

    result = deepcopy(layout)
    if not isinstance(result, dict):
        return None
    pending = [result]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            application_id = current.get("application_id")
            if isinstance(application_id, str) and application_id in {"mindmap-workbench", "workbench"}:
                current["application_id"] = "mindmap"
            if current.get("kind") == "tabs" and isinstance(current.get("tokens"), list):
                tokens = current["tokens"]
                index = current.get("current", 0)
                index = index if type(index) is int else 0
                selected = tokens[index] if 0 <= index < len(tokens) else None
                current["tokens"] = [token for token in tokens if isinstance(token, str)
                                     and token not in retired_ids and not retired_tool(token.removeprefix("tool:"))]
                current["current"] = (current["tokens"].index(selected) if selected in current["tokens"] else 0)
            if isinstance(current.get("tools"), list):
                current["tools"] = [tool for tool in current["tools"]
                                    if not isinstance(tool, dict) or not retired_tool(tool.get("id"))]
            active = current.get("active")
            if isinstance(active, str) and (retired_tool(active) or active in retired_ids):
                current["active"] = ""
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return result


def _convert_state(old: dict, layout) -> dict:
    """只恢复有明确导图路径的视图；其他业务的来源和状态归档，不能猜测节点归属。"""
    views, retired, seen = [], [], set()
    records = old.get("views", old.get("open_documents", []))
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        path, identifier = record.get("path"), record.get("id")
        if (not isinstance(path, str) or not Path(path).is_absolute()
                or not isinstance(identifier, str) or not identifier or identifier in seen):
            continue
        seen.add(identifier)
        if record.get("plugin_id", "mindmap") != "mindmap" or Path(path).suffix.lower() != ".mindmap":
            retired.append(deepcopy(record))
            continue
        view = {"id": identifier, "path": path}
        if isinstance(record.get("document_id"), str) and record["document_id"]:
            view["document_id"] = record["document_id"]
        if isinstance(record.get("view_state"), dict):
            view["view_state"] = deepcopy(record["view_state"])
        views.append(view)
    recent = old.get("recent_files", [])
    recent = [path for path in recent if isinstance(path, str) and Path(path).is_absolute()] if isinstance(recent, list) else []
    components = old.get("plugins", {})
    components = components if isinstance(components, dict) else {}
    mindmap = deepcopy(components.get("mindmap", {}))
    if not isinstance(mindmap, dict):
        mindmap = {}
    if "recent_pages" in old:
        mindmap["page_history"] = {"schema_version": 1, "recent_pages": deepcopy(old["recent_pages"])}
    archive = {
        "views": retired,
        "sources": list(dict.fromkeys([*(record["path"] for record in retired),
                                        *(path for path in recent if Path(path).suffix.lower() != ".mindmap")])),
        "component_state": {key: deepcopy(value) for key, value in components.items() if key != "mindmap"},
    }
    if "legacy_software_structure" in mindmap:
        archive["software_structure"] = mindmap.pop("legacy_software_structure")
    identifiers = {view["id"] for view in views}
    active = old.get("active_id", "")
    return {
        "schema_version": 2, "views": views,
        "active_id": active if isinstance(active, str) and active in identifiers else "",
        "recent_files": [path for path in recent if Path(path).suffix.lower() == ".mindmap"][:20],
        "mindmap": mindmap, "layout": _convert_layout(layout, {record["id"] for record in retired}),
        "migration_archive": archive,
    }


def migrate_legacy(directory) -> tuple[dict | None, dict]:
    """在新配置缺失时提供转换后的初始数据，不写回、移动或删除历史文件。

    状态和设置分别判断是否需要导入，避免其中一个新文件已存在时重复覆盖。
    所有读取仅针对已知的本机配置名；架构来源文件由用户在目标节点中导入。
    """
    directory = Path(directory)
    source = _source_directory(directory)
    state, settings = None, {}
    if not (directory / "mindmap-state-v2.json").exists():
        old = _read_object(source / "workbench-state-v1.json")
        if old is None:
            old = _read_object(source / "documents-v1.json")
        if old and old.get("schema_version") == 1:
            layout = old.get("layout")
            if old.get("session_checkpoint") is not True:
                layout = _read_object(source / "workbench-layout-v1.json") or layout
                layout = layout or _read_object(source / "layouts-v1.json")
            state = _convert_state(old, layout)
    if not (directory / "mindmap-settings-v1.json").exists():
        old = _read_object(source / "workbench-settings-v1.json")
        if old and old.get("schema_version") == 1 and isinstance(old.get("values"), dict):
            # 窗口和导图是两个内部偏好分组，插件开关及其他业务设置不进入新配置。
            settings = {key: deepcopy(value) for key, value in old["values"].items()
                        if key in {"", "mindmap"} and isinstance(value, dict)}
        elif (source / "preferences.ini").is_file():
            previous = QSettings(str(source / "preferences.ini"), QSettings.Format.IniFormat)
            for key in previous.allKeys():
                settings.setdefault("" if key.startswith("window/") else "mindmap", {})[key] = previous.value(key)
    return state, settings
