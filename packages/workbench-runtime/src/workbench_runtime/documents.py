"""统一管理共享文档和标签；业务模型及文件编码始终归编辑器插件。"""

from collections import defaultdict
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QLabel, QMessageBox, QWidget
from workbench_api import Disposable, OperationResult, SavePolicy, path_key

from .ui.docking import DocumentSpec


class DocumentService(QObject):
    """全部插件共用的视图登记、资源去重、保存和两阶段关闭协调器。"""

    active_changed = Signal(object)
    changed = Signal()

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.views = {}
        self.providers = {}
        self.owners = {}
        self._file_qualifiers = {}
        self.active_id = ""
        self._prepared = False
        self._switching = False
        window.manager.document_activated.connect(self.activate)
        window.manager.close_guard = self.prepare_close

    @property
    def active(self):
        return self.views.get(self.active_id)

    def activate_view(self, view_id: str) -> None:
        """选中现有标签并聚焦其控件，由停靠信号同步活动视图。"""
        self.window.manager.activate_document(view_id)

    def find_path(self, path):
        key = path_key(path)
        if self.active and self.active.document.path and path_key(self.active.document.path) == key:
            return self.active
        return next((view for view in self.ordered() if view.document.path and
                     path_key(view.document.path) == key), None)

    def ordered(self):
        ids = []

        def visit(node):
            if node["kind"] == "tabs":
                ids.extend(node["tokens"])
            else:
                for child in node["children"]:
                    visit(child)

        visit(self.window.host.editor.snapshot())
        return [self.views[key] for key in ids if key in self.views]

    def register_view(self, provider_id, view, *, after=None, remember=True):
        """成功初始化后才能注册；安装异常回滚登记，调用方仍负责释放候选业务对象。"""
        if not isinstance(view.widget, QWidget) or view.id in self.views:
            raise ValueError("编辑器控件无效或视图标识重复")
        existing = self.find_path(view.document.path) if view.document.path else None
        if existing is not None and not isinstance(existing, MissingView) and existing.document is not view.document:
            raise ValueError("文件已绑定其他文档，请为已有共享文档创建视图")
        if any(item.document.id == view.document.id and item.document is not view.document for item in self.views.values()):
            raise ValueError("不同文档不能复用同一文档标识")
        owner, _provider = self.window.providers[provider_id]
        area = self.window.host.editor
        anchor = area.find_token(after) if after else None
        target = anchor or area.find_token(self.active_id)
        self.views[view.id], self.providers[view.id], self.owners[view.id] = view, provider_id, owner
        try:
            self.window.manager.register_document(DocumentSpec(
                view.id, view.title, lambda: view.widget, icon=getattr(view, "icon", "document"),
                can_close=lambda: self.prepare_close([view.id]), on_closed=lambda: self._closed(view.id),
                dispose_on_close=True, on_duplicate=lambda: bool(view.duplicate()),
                on_open_directory=lambda: self.window.open_file_directory(view.id),
            ), group_id=target[0].group_id if target else "", index=anchor[1] + 1 if anchor else -1)
            self.window.manager.set_document_actions(view.id, getattr(view, "toolbar_actions", ()))
            for action in getattr(view, "edit_actions", {}).values():
                action.changed.connect(self.window.edit_commands.refresh)
            self.sync(view)
            self.activate_view(view.id)
            if remember and view.document.path:
                self.window.remember_path(view.document.path)
        except Exception:
            if view.id in self.window.manager.documents:
                self.remove_view(view.id)
            self.views.pop(view.id, None)
            self.providers.pop(view.id, None)
            self.owners.pop(view.id, None)
            raise
        # 注销句柄只捕获标识，不能在插件整个生命周期内持有已关闭视图及其大型文档。
        return Disposable(lambda view_id=view.id: self.remove_view(view_id))

    def remove_view(self, view_id):
        """回滚或插件关闭时强制释放，不触发保存；正常用户关闭必须经过 close_guard。"""
        if view_id not in self.views:
            return
        guard, self.window.manager.close_guard = self.window.manager.close_guard, lambda ids: True
        try:
            self.window.manager.close_document(view_id, dispose=True)
        finally:
            self.window.manager.close_guard = guard

    def _closed(self, view_id):
        view = self.views.pop(view_id, None)
        self.providers.pop(view_id, None)
        self.owners.pop(view_id, None)
        if view is not None:
            view.dispose()
        self._update_file_qualifiers()
        if self.active_id == view_id:
            self.active_id = ""
            self.active_changed.emit(None)
        self.changed.emit()

    def activate(self, view_id):
        if self._switching or view_id == self.active_id:
            return
        self._switching = True
        try:
            if self.active:
                self.active.prepare_input()
            self.active_id = view_id
            self.active_changed.emit(self.active)
            self.changed.emit()
        finally:
            self._switching = False

    def sync(self, view):
        if view.id not in self.views:
            return
        if not self._update_file_qualifiers():
            self._sync_tab(view)
        self.window.manager.schedule_save()
        self.changed.emit()

    def display_title(self, view, *, file_name=False) -> str:
        """为同名文件附加最短可区分的目录。标题栏可只借用文件名和区分信息。

        同一文件的多个视图共用一个名称。同目录的同名异格式文件使用扩展名区分。
        """
        path = view.document.path
        title = path.stem if file_name and path is not None else view.title
        qualifier = self._file_qualifiers.get(path, "")
        return f"{title} · {qualifier}" if qualifier else title

    def _update_file_qualifiers(self) -> bool:
        paths = {view.document.path for view in self.views.values() if view.document.path is not None}
        if paths == self._file_qualifiers.keys():
            return False
        self._file_qualifiers = dict.fromkeys(paths, "")
        by_name = defaultdict(list)
        for path in paths:
            by_name[path.stem.casefold()].append(path)
        for same_name in by_name.values():
            if len(same_name) < 2:
                continue
            for path in same_name:
                parents = {other.parent for other in same_name if other.parent != path.parent}
                parts = path.parent.parts
                directory = ""
                if parents:
                    for length in range(1, len(parts) + 1):
                        suffix = Path(*parts[-length:])
                        if all(suffix != Path(*parent.parts[-length:]) for parent in parents):
                            directory = str(suffix)
                            break
                    directory = directory or str(path.parent)
                extension = path.suffix if any(other != path and other.parent == path.parent
                                               for other in same_name) else ""
                self._file_qualifiers[path] = " · ".join(value for value in (directory, extension) if value)
        for view in self.views.values():
            self._sync_tab(view)
        return True

    def _sync_tab(self, view):
        manager = self.window.manager
        manager.set_document_title(view.id, self.display_title(view))
        manager.set_document_modified(view.id, view.document.is_dirty)
        message = getattr(view.document, "last_error", "")
        tooltip = str(view.document.path or "未保存的文档")
        manager.set_document_tooltip(view.id, tooltip + (f"\n保存失败：{message}" if message else ""))

    def view_state_changed(self, view_id: str) -> None:
        """合并已登记视图的现场更新，防抖到期后随标签和布局保存。"""
        if view_id in self.views:
            self.window.manager.schedule_save()

    @staticmethod
    def can_save(view) -> bool:
        """判断视图是否持有可保存的文档，供文件动作和批量保存统一使用。"""
        return (view is not None and not isinstance(view, MissingView)
                and view.document.save_policy != SavePolicy.READ_ONLY)

    def save(self, view=None, *, save_as=False, reason="explicit"):
        view = view or self.active
        if not self.can_save(view):
            return OperationResult.cancelled()
        previous_path = view.document.path
        try:
            result = view.document.save(save_as=save_as, reason=reason)
        except Exception as error:
            result = OperationResult.failed(error)
        if result and view.document.path and (save_as or previous_path != view.document.path):
            self.window.remember_path(view.document.path)
        if not result and result.message:
            self.window.show_status_message(result.message)
        elif result and reason == "explicit":
            self.window.show_status_message(f"已保存：{view.document.path}")
        for item in self.views.values():
            if item.document is view.document:
                self.sync(item)
        return result

    def save_all(self):
        seen = set()
        for view in self.ordered():
            if not self.can_save(view) or view.document.id in seen:
                continue
            seen.add(view.document.id)
            result = self.save(view, reason="batch")
            if not result:
                self.activate_view(view.id)
                return result
        self.window.record_operation(f"保存全部完成，共 {len(seen)} 份文档")
        self.window.show_status_message("全部文档已保存")
        return OperationResult()

    def prepare_close(self, ids):
        """先检查整批输入，再逐文档决策；放弃不修改文档，实际关闭后才释放。

        同一文件仍有未关闭视图时，手动保存文档无需询问。自动保存文档沿用
        关闭即保存规则。任何失败只中止关闭，不撤销此前已经成功的文件写入。
        """
        if self._prepared:
            return True
        targets = [self.views[key] for key in ids if key in self.views]
        for view in targets:
            if not view.prepare_input():
                self.activate_view(view.id)
                return False
        seen = set()
        for view in targets:
            document = view.document
            if not self.can_save(view) or document.id in seen:
                continue
            seen.add(document.id)
            if document.save_policy == SavePolicy.AUTOMATIC:
                if not self.save(view, reason="close"):
                    self.activate_view(view.id)
                    return False
            elif document.is_dirty and not any(item.document is document and item.id not in ids
                                                for item in self.views.values()):
                answer = self.window.confirm_close(view)
                if answer == QMessageBox.StandardButton.Cancel:
                    self.activate_view(view.id)
                    return False
                if answer == QMessageBox.StandardButton.Save and not self.save(view, reason="close"):
                    self.activate_view(view.id)
                    return False
        return True

    def snapshot(self):
        records = []
        for view in self.ordered():
            if isinstance(view, MissingView):
                records.append(deepcopy(view.record))
            elif view.document.path:
                records.append({"id": view.id, "document_id": view.document.id,
                                "plugin_id": self.owners[view.id], "editor_type": self.providers[view.id],
                                "path": str(view.document.path), "view_state": view.capture_state()})
        return records

    def restore(self, records, active_id):
        failures = []
        for record in records:
            try:
                entry = self.window.providers.get(record["editor_type"])
                if entry is None or entry[0] != record["plugin_id"]:
                    raise ValueError("所属插件尚未启用或未安装")
                result = entry[1].open_document(Path(record["path"]), record["id"], record.get("view_state"))
                if not result:
                    raise ValueError(result.message or "打开已取消")
                if record["id"] in self.views and record.get("document_id"):
                    self.views[record["id"]].document.id = record["document_id"]
            except Exception as error:
                failures.append(f"{Path(record['path']).name}：{error}")
                view = MissingView(record, str(error))
                self.views[view.id], self.providers[view.id] = view, record["editor_type"]
                self.owners[view.id] = record["plugin_id"]
                self.window.manager.register_document(DocumentSpec(
                    view.id, view.title, lambda item=view: item.widget, dispose_on_close=True,
                    on_closed=lambda key=view.id: self._closed(key), can_close=lambda: True,
                ))
                self.sync(view)
        selected = self.views.get(active_id)
        if selected is None or isinstance(selected, MissingView):
            selected = next((view for view in self.views.values() if not isinstance(view, MissingView)), selected)
        if selected is not None:
            self.activate_view(selected.id)
        return failures

    def dispose(self):
        for key in list(self.views):
            self._closed(key)


class MissingView:
    """缺失插件或文件的可关闭占位；原始恢复记录逐字段保留，下次启动可继续恢复。"""

    def __init__(self, record, error):
        self.record = deepcopy(record)
        self.id = record["id"]
        self.document = self
        self.path = Path(record["path"])
        self.title = self.path.stem + "（暂不可用）"
        self.is_dirty = False
        self.save_policy = SavePolicy.MANUAL
        self.widget = QLabel(f"暂时无法打开 {self.path.name}\n\n{error}\n\n原有视图记录已保留。")
        self.widget.setWordWrap(True)

    def prepare_input(self):
        return True

    def dispose(self):
        self.widget.deleteLater()
