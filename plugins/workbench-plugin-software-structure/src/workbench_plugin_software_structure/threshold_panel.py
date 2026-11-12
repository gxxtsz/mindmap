"""工作台右侧的公共依赖阈值面板，始终编辑活动软件结构文件。"""

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import QFormLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout, QWidget
from workbench_api import Disposable, EditorView, PluginContext
from workbench_api.qt import PanelSpec

from .document import MAX_COMPONENT_SIZE, PublicDependencyThresholds
from .viewer import SoftwareStructureDocument, SoftwareStructureView

_PANEL_ID = "software-structure.public-dependencies"


class PublicDependencyThresholdPanel(QWidget):
    """编辑绑定文件的阈值。保存时机和成功值由共享文档管理。"""

    def __init__(self):
        """创建两个数值输入和默认值入口，由宿主提供停靠外壳及主题。"""
        super().__init__()
        self._document: SoftwareStructureDocument | None = None
        self.setObjectName("software_structure_public_dependency_panel")
        self.setAccessibleName("公共依赖阈值")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        layout.addLayout(form)

        self._component_size = QSpinBox(self)
        self._component_size.setObjectName("public_dependency_component_size")
        self._component_size.setRange(0, MAX_COMPONENT_SIZE)
        form.addRow("子图模块数超过", self._component_size)
        self.setFocusProxy(self._component_size)

        self._percent = QSpinBox(self)
        self._percent.setObjectName("public_dependency_direct_dependent_percent")
        self._percent.setRange(0, 100)
        self._percent.setSuffix("%")
        form.addRow("直接依赖比例超过", self._percent)

        hint = QLabel("在每个连通子图中计算直接依赖该模块的其他模块占比。", self)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        restore = QPushButton("恢复默认", self)
        restore.setObjectName("public_dependency_restore_defaults")
        restore.clicked.connect(self._restore_defaults)
        layout.addWidget(restore, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()

        self._component_size.valueChanged.connect(self._queue_thresholds)
        self._percent.valueChanged.connect(self._queue_thresholds)
        self._sync_fields()
        self.setEnabled(False)

    def bind_document(self, document: SoftwareStructureDocument | None) -> None:
        """随活动标签绑定共享文档，切换或停用时释放旧文档的信号订阅。"""
        if document is self._document:
            return
        if self._document is not None:
            self._document.thresholds_changed.disconnect(self._sync_fields)
            self._document.threshold_save_failed.disconnect(self._sync_fields)
        self._document = document
        if document is not None:
            document.thresholds_changed.connect(self._sync_fields)
            document.threshold_save_failed.connect(self._sync_fields)
        self._sync_fields()
        self.setEnabled(document is not None)

    def _set_fields(self, thresholds: PublicDependencyThresholds) -> None:
        # 回显和恢复默认同时设置两个输入。暂时屏蔽信号，避免为中间值排队保存。
        with QSignalBlocker(self._component_size), QSignalBlocker(self._percent):
            self._component_size.setValue(thresholds.component_size)
            self._percent.setValue(thresholds.direct_dependent_percent)

    def _sync_fields(self, *_) -> None:
        """从已提交的快照回显数值，保存失败时也使用这一入口恢复侧栏。"""
        thresholds = (self._document.content.public_dependency_thresholds
                      if self._document is not None else PublicDependencyThresholds())
        self._set_fields(thresholds)

    def _restore_defaults(self) -> None:
        self._set_fields(PublicDependencyThresholds())
        self._queue_thresholds()

    def _queue_thresholds(self) -> None:
        if self._document is not None:
            self._document.queue_public_dependency_thresholds(
                PublicDependencyThresholds(self._component_size.value(), self._percent.value()),
            )


def register_public_dependency_panel(context: PluginContext) -> Disposable:
    """登记右侧面板。暂停非软件结构文件的入口，保留用户设定的显隐及停靠位置。"""
    panel: PublicDependencyThresholdPanel | None = None

    def create_panel() -> QWidget:
        """首次展开时由宿主创建内容，并读取当前活动文件的阈值。"""
        nonlocal panel
        panel = PublicDependencyThresholdPanel()
        active = context.documents.active
        if isinstance(active, SoftwareStructureView):
            panel.bind_document(active.document)
        return panel

    def bind_view(view: EditorView | None) -> None:
        document = view.document if isinstance(view, SoftwareStructureView) else None
        if panel is not None:
            panel.bind_document(document)
        context.ui.set_panels_suspended((_PANEL_ID,), document is None)

    context.ui.register_panel(PanelSpec(
        id=_PANEL_ID, title="公共依赖", factory=create_panel, icon="settings", position="right_top",
    ))
    detach = context.own(lambda: panel.bind_document(None) if panel is not None else None)
    subscription = context.ui.subscribe_active(bind_view)
    bind_view(context.documents.active)

    def dispose() -> None:
        subscription.dispose()
        detach.dispose()

    return Disposable(dispose)
