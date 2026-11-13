"""通过仅供测试的编辑器验收通用宿主的应用和文档协议。"""

from pathlib import Path
from uuid import uuid4

import pytest
from mindmap_desktop_api import OperationResult, SavePolicy
from mindmap_desktop_runtime import DesktopRuntimeWindow
from PySide6.QtCore import QObject
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QFileDialog, QPlainTextDocumentLayout, QPlainTextEdit


class SampleDocument(QTextDocument):
    """提供最小的手动保存文档，多个视图共用一份 Qt 内容及修改状态。"""

    save_policy = SavePolicy.MANUAL

    def __init__(self, application, path=None):
        super().__init__(application)
        self.id = uuid4().hex
        self.path = Path(path).resolve() if path else None
        self.setDocumentLayout(QPlainTextDocumentLayout(self))
        if self.path is not None:
            self.setPlainText(self.path.read_text("utf-8"))
        self.setModified(False)

    @property
    def is_dirty(self):
        return self.isModified()

    @property
    def resource_uri(self):
        return self.path.as_uri() if self.path else "untitled:" + self.id

    def save(self, *, save_as=False, reason="explicit"):
        """首次保存由测试注入目标，写入成功后才更新路径及修改状态。"""
        target = self.path
        if target is None or save_as:
            filename, _ = QFileDialog.getSaveFileName(self.parent().context.ui.parent, "保存示例")
            if not filename:
                return OperationResult.cancelled()
            target = Path(filename)
        target.write_text(self.toPlainText(), "utf-8")
        self.path = target
        self.setModified(False)
        return OperationResult()


class SampleView:
    """每个标签持有独立控件，通过文档服务登记、复制和同步。"""

    def __init__(self, application, document, view_id):
        self.id = view_id or "sample." + uuid4().hex
        self.application = application
        self.document = document
        self.widget = QPlainTextEdit()
        self.widget.setDocument(document)
        self.edit_actions = {}
        document.modificationChanged.connect(self.sync)

    @property
    def title(self):
        return self.document.path.stem if self.document.path else "未命名"

    def sync(self):
        self.application.context.documents.sync(self)

    def prepare_input(self):
        return True

    def capture_state(self):
        return {}

    def restore_state(self, state):
        """测试视图无需恢复控件状态。"""

    def duplicate(self):
        return self.application.add_view(self.document, after=self.id, remember=False)

    def dispose(self):
        self.document.modificationChanged.disconnect(self.sync)
        self.widget.deleteLater()


class SampleApplication(QObject):
    """提供导图应用测试替身，所有视图通过公开接口进入真实宿主。"""

    def __init__(self, context):
        super().__init__(context.ui.parent)
        self.context = context

    def new_document(self, path=None):
        return self.add_view(SampleDocument(self))

    def open_document(self, path, view_id=None, state=None):
        """每次构造候选文档，便于测试宿主拒绝同一资源的重复登记。"""
        return self.add_view(SampleDocument(self, path), view_id=view_id, remember=view_id is None)

    def add_view(self, document, *, view_id=None, after=None, remember=True):
        view = SampleView(self, document, view_id)
        try:
            self.context.documents.register_view(view, after=after, remember=remember)
        except Exception:
            view.dispose()
            raise
        return OperationResult(view_id=view.id)

    def dispose(self):
        self.deleteLater()


@pytest.fixture
def application_factory():
    return SampleApplication


@pytest.fixture
def window(qtbot, tmp_path, application_factory):
    """创建隔离配置的真实窗口；测试后释放后台对象和全部应用订阅。"""
    result = DesktopRuntimeWindow(storage_dir=tmp_path / "配置", restore=False, application_factory=application_factory)
    qtbot.addWidget(result, before_close_func=lambda item: item.dispose())
    result.show()
    yield result
    result.dispose()


@pytest.fixture
def workspace(window):
    """沿用迁入用例的夹具名称，确保同一测试始终复用同一个窗口。"""
    return window
