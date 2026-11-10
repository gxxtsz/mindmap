"""插件发现、依赖排序、激活回滚及重启生效的插件管理界面。"""

from collections import Counter
from dataclasses import dataclass
from importlib.metadata import entry_points

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)
from workbench_api import API_VERSION, PluginManifest


@dataclass
class PluginRecord:
    id: str
    name: str
    version: str
    manifest: PluginManifest | None = None
    enabled: bool = True
    status: str = "待加载"
    error: str = ""
    instance: object = None
    context: object = None


class PluginManager:
    """仅导入轻量入口；业务激活按依赖拓扑进行，异常不会截断整个应用启动。"""

    def __init__(self, context_factory, settings, manifests=None):
        self.context_factory, self.settings = context_factory, settings
        self.records = []
        self.order = []
        if manifests is None:
            for entry in sorted(entry_points(group="personal_workbench.plugins"), key=lambda item: item.name):
                try:
                    manifest = entry.load()
                    if not isinstance(manifest, PluginManifest) or manifest.id != entry.name:
                        raise ValueError("入口标识与插件清单不一致")
                    self.records.append(PluginRecord(manifest.id, manifest.name, manifest.version, manifest))
                except Exception as error:
                    self.records.append(PluginRecord(entry.name, entry.name, "", status="加载失败", error=str(error)))
        else:
            self.records = [PluginRecord(item.id, item.name, item.version, item) for item in manifests]
        duplicates = {key for key, count in Counter(item.id for item in self.records).items() if count > 1}
        for record in self.records:
            record.enabled = self.settings.value(f"enabled/{record.id}", True) is not False
            if record.id in duplicates:
                record.error, record.status = "插件标识重复", "加载失败"

    def activate(self):
        by_id = {record.id: record for record in self.records}
        visiting = set()

        def visit(record):
            if record.status != "待加载":
                return record.status == "已启用"
            if not record.enabled:
                record.status = "已禁用"
                return False
            manifest = record.manifest
            if manifest.api_version != API_VERSION:
                record.status, record.error = "加载失败", "插件 API 版本不兼容"
                return False
            if record.id in visiting:
                record.status, record.error = "加载失败", "插件依赖存在循环"
                return False
            visiting.add(record.id)
            for dependency in manifest.requires:
                if dependency not in by_id or not visit(by_id[dependency]):
                    record.status, record.error = "加载失败", f"必需插件不可用：{dependency}"
                    visiting.discard(record.id)
                    return False
            visiting.discard(record.id)
            context = None
            try:
                context = self.context_factory(record.id)
                record.instance = manifest.activate(context)
                record.context, record.status = context, "已启用"
                self.order.append(record)
                return True
            except Exception as error:
                if context is not None:
                    context.dispose()
                record.status, record.error = "加载失败", str(error)
                return False

        for record in self.records:
            visit(record)

    def instance(self, plugin_id):
        return next((item.instance for item in self.records if item.id == plugin_id and item.status == "已启用"), None)

    def dispose(self):
        for record in reversed(self.order):
            try:
                if record.instance is not None:
                    record.instance.dispose()
            except Exception as error:
                record.context.log(f"插件释放失败：{record.name}：{error}", level="error")
            finally:
                record.context.dispose()
        self.order.clear()

    def show_dialog(self, parent):
        dialog = QDialog(parent)
        dialog.setWindowTitle("插件管理")
        dialog.resize(720, 340)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("启用状态将在下次启动时生效。", dialog))
        tree = QTreeWidget(dialog)
        tree.setHeaderLabels(["插件", "版本", "当前状态", "说明"])
        tree.setRootIsDecorated(False)
        for column in range(3):
            tree.header().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for record in self.records:
            item = QTreeWidgetItem([record.name, record.version, record.status, record.error])
            item.setData(0, Qt.ItemDataRole.UserRole, record.id)
            item.setToolTip(0, f"{record.name}（{record.id}）")
            item.setToolTip(3, record.error)
            item.setCheckState(0, Qt.CheckState.Checked if self.settings.value(f"enabled/{record.id}", True)
                               else Qt.CheckState.Unchecked)
            tree.addTopLevelItem(item)
        layout.addWidget(tree)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            for index in range(tree.topLevelItemCount()):
                item = tree.topLevelItem(index)
                self.settings.setValue(f"enabled/{item.data(0, Qt.ItemDataRole.UserRole)}",
                                       item.checkState(0) == Qt.CheckState.Checked)
            self.settings.sync()
