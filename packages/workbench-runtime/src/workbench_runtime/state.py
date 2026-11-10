"""版本化工作台状态；旧数据只由产品兼容层转换，不在运行时解释业务格式。"""

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from workbench_api import path_key

from .storage import JsonLayoutStore


class Settings:
    """插件命名空间设置，同时提供小型 QSettings 兼容接口以平稳迁移组件。"""

    def __init__(self, store, values, namespace=""):
        self.store, self.values, self.namespace = store, values, namespace

    def scoped(self, namespace):
        return Settings(self.store, self.values, namespace)

    def value(self, key, default=None):
        return deepcopy(self.values.get(self.namespace, {}).get(key, default))

    def setValue(self, key, value):
        self.values.setdefault(self.namespace, {})[key] = deepcopy(value)

    def sync(self):
        self.store.save({"schema_version": 1, "values": self.values})

    @classmethod
    def load(cls, store, initial):
        """坏设置备份后降级启动；备份失败则拒绝后续覆盖，避免丢失可修复内容。"""
        error = ""
        try:
            payload = store.load()
            if payload is not None and (payload.get("schema_version") != 1 or
                                       not isinstance(payload.get("values"), dict) or
                                       not all(isinstance(value, dict) for value in payload["values"].values())):
                raise ValueError("工作台设置格式或版本无效")
        except (OSError, ValueError, TypeError) as caught:
            error = f"工作台设置无效，已保留原记录：{caught}"
            try:
                store.path.with_name(f"workbench-settings-invalid-{uuid4().hex}.json").write_bytes(store.path.read_bytes())
            except OSError:
                def reject_save(payload):
                    raise OSError("原设置无法备份，暂时禁止覆盖")
                store.save = reject_save
            payload = None
        return cls(store, payload["values"] if payload else initial), error


class StateStore:
    """工作台的本机状态。标签、活动文档与分屏布局作为同一份会话快照提交。

    最近资源立即落盘，启动恢复期间由宿主设置 restoring 屏障。
    配置只存工作现场及插件元数据。写入失败时旧磁盘快照仍完整可读。
    """

    def __init__(self, directory, initial=None):
        self.store = JsonLayoutStore(Path(directory) / "workbench-state-v1.json")
        self.restoring = True
        self.error = ""
        self.read_only = False
        self.data = {"schema_version": 1, "views": [], "active_id": "", "recent_files": [],
                     "plugins": {}, "layout": None}
        try:
            payload = self.store.load()
            if payload is not None:
                self._validate(payload)
        except (OSError, ValueError, TypeError) as error:
            self.error = f"工作台状态无效，已保留原记录：{error}"
            # 已损坏的新配置先保留字节副本，再允许写入新会话。
            # 备份失败时保持只读，避免用空会话覆盖用户尚可修复的数据。
            try:
                backup = self.store.path.with_name(f"workbench-state-invalid-{uuid4().hex}.json")
                backup.write_bytes(self.store.path.read_bytes())
            except OSError:
                self.read_only = True
            payload = None
        if payload is None:
            payload = initial or self.data
        self._validate(payload)
        self.data.update(deepcopy(payload))

    @staticmethod
    def _validate(payload):
        if payload.get("schema_version") != 1:
            raise ValueError("不支持的工作台状态版本")
        if not isinstance(payload.get("active_id", ""), str):
            raise ValueError("活动编辑器标识无效")
        views, recent = payload.get("views", []), payload.get("recent_files", [])
        if not isinstance(views, list) or not isinstance(recent, list):
            raise ValueError("工作台资源记录无效")
        seen, document_resources, resource_documents = set(), {}, {}
        for item in views:
            if not isinstance(item, dict) or any(not isinstance(item.get(key), str) or not item[key]
                                                for key in ("id", "plugin_id", "editor_type", "path")):
                raise ValueError("编辑器恢复记录无效")
            if item["id"] in seen or not Path(item["path"]).is_absolute():
                raise ValueError("编辑器标识重复或资源路径不是绝对路径")
            seen.add(item["id"])
            # 文档标识用于保存去重，必须在恢复前验证类型及资源对应关系。
            # 同一资源的多视图可以复用标识；旧迁移记录未带文档标识时仍可打开。
            if "document_id" in item:
                document_id = item["document_id"]
                if not isinstance(document_id, str) or not document_id:
                    raise ValueError("共享文档标识无效")
                resource = path_key(item["path"])
                if (document_resources.get(document_id, resource) != resource or
                        resource_documents.get(resource, document_id) != document_id):
                    raise ValueError("共享文档标识与资源对应关系冲突")
                document_resources[document_id] = resource
                resource_documents[resource] = document_id
        if not all(isinstance(item, str) and Path(item).is_absolute() for item in recent):
            raise ValueError("最近资源路径无效")
        if (not isinstance(payload.get("plugins", {}), dict) or
                not all(isinstance(value, dict) for value in payload.get("plugins", {}).values())):
            raise ValueError("插件状态无效")

    @property
    def recent_files(self):
        return self.data["recent_files"]

    @property
    def open_documents(self):
        return self.data["views"]

    @property
    def active_id(self):
        return self.data["active_id"]

    def save(self):
        if not self.restoring and not self.read_only:
            self.store.save(self.data)

    def remember(self, path):
        path = str(Path(path).resolve())
        self.data["recent_files"] = [path, *(item for item in self.recent_files
                                            if path_key(item) != path_key(path))][:20]
        self.save()

    def clear_recent(self):
        self.data["recent_files"] = []
        self.save()

    def save_session(self, views, active_id, layout=None):
        """原子提交一轮工作现场。写入失败时恢复内存中的上一轮记录。

        layout 提供时同时标记新快照为权威版本，使启动时跳过遗留的单独布局文件。
        调用方可继续省略 layout 以保留已有布局。传入的嵌套记录由状态层独立持有。
        """
        previous = self.data.copy()
        self.data["views"] = deepcopy(views)
        self.data["active_id"] = active_id
        if layout is not None:
            self.data["layout"] = deepcopy(layout)
            self.data["session_checkpoint"] = True
        try:
            self.save()
        except (OSError, TypeError, ValueError):
            self.data = previous
            raise

    def plugin_state(self, plugin_id):
        return PluginState(self, plugin_id)


class PluginState:
    """业务可保存 JSON 状态，不读取其他插件的命名空间。"""

    def __init__(self, owner, plugin_id):
        self.owner, self.plugin_id = owner, plugin_id

    def get(self, key, default=None):
        return deepcopy(self.owner.data["plugins"].get(self.plugin_id, {}).get(key, default))

    def set(self, key, value):
        self.owner.data["plugins"].setdefault(self.plugin_id, {})[key] = deepcopy(value)

    def flush(self):
        self.owner.save()


class WorkspaceLayoutStore:
    """停靠框架与工作台会话快照之间的适配层，旧布局文件仅用于首次读取。"""

    def __init__(self, state, capture_documents):
        self.state = state
        self.capture_documents = capture_documents
        self.path = state.store.path
        self.legacy_layout = JsonLayoutStore(self.path.with_name("workbench-layout-v1.json"))
        self.default_tools = []

    def load(self):
        if self.state.data.get("session_checkpoint") is not True and self.legacy_layout.path.exists():
            payload = self.legacy_layout.load()
        else:
            payload = deepcopy(self.state.data.get("layout"))
        if not isinstance(payload, dict) or not isinstance(payload.get("session"), dict):
            return payload
        tools = payload["session"].get("tools")
        if not isinstance(tools, list) or not all(isinstance(item, dict) for item in tools):
            return payload
        # 新增的默认面板通过注册元数据补入历史布局，不在平台中判断插件类型。
        for default in self.default_tools:
            if any(item.get("id") == default["id"] for item in tools):
                continue
            item = deepcopy(default)
            for other in tools:
                if other.get("position") == item["position"] and other.get("host_id") == item["host_id"]:
                    other["visible"] = False
            tools.append(item)
            payload["session"]["active"] = item["id"]
        return payload

    def save(self, payload):
        """在 GUI 线程读取关联文档及活动标识，与本次布局一并原子提交。"""
        if not self.state.restoring:
            views, active_id = self.capture_documents()
            self.state.save_session(views, active_id, payload)
