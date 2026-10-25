"""文件能力公开检查不提交输入，也不改变引擎或保存位置。"""

import pytest
from mindmap_core import FormatInfo, FormatRegistry, MindMapEngine, MindMapError
from mindmap_formats import MindmapCodec
from mindmap_qt_canvas import MindMapFileController, MindMapFileState, MindMapWidget
from PySide6.QtWidgets import QFileDialog


def test_file_controllers_can_share_path_and_cancel_before_submitting(qtbot, tmp_path, monkeypatch):
    """共享状态只在成功写入后改绑，准备失败不提交输入，旧宿主默认仍独立。"""
    engine = MindMapEngine()
    canvases = [MindMapWidget(engine), MindMapWidget(engine)]
    for canvas in canvases:
        qtbot.addWidget(canvas)
    calls, errors = [], []

    class Inputs:
        def flush_pending_edits(self):
            calls.append(True)

    state = MindMapFileState(tmp_path / "原图.mindmap")
    first, second = [MindMapFileController(canvas, Inputs(), state=state) for canvas in canvases]
    independent = MindMapFileController(canvases[0], Inputs())
    target = tmp_path / "改绑.mindmap"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a: (str(target), ""))
    first.prepare_save = lambda: False
    try:
        assert not first.save(save_as=True) and not calls and not target.exists()
        first.prepare_save = lambda: True
        assert first.save(save_as=True)
        assert first.path == second.path == target and independent.path is None
        second.error_occurred.connect(errors.append)
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a: (str(tmp_path / "错误.txt"), ""))
        assert not second.save(save_as=True)
        assert first.path == second.path == target and errors
    finally:
        for canvas in canvases:
            canvas.dispose()


def test_native_capability_check_is_pure_and_rejects_one_way_codec(qtbot):
    canvas = MindMapWidget()
    qtbot.addWidget(canvas)

    class Inputs:
        def flush_pending_edits(self):
            raise AssertionError("格式预检不能提交输入")

    files = MindMapFileController(canvas, Inputs())
    before = canvas.engine.document, canvas.engine.revision, files.path
    try:
        assert isinstance(files.require_native_save_format().exporter, MindmapCodec)
        for mode in ("missing", "importer", "exporter"):
            canvas.format_registry = FormatRegistry()
            if mode != "missing":
                canvas.format_registry.register(FormatInfo("mindmap", "原生", (".mindmap",)), **{mode: MindmapCodec()})
            with pytest.raises(MindMapError, match="原生编解码器"):
                files.require_native_save_format()
            assert (canvas.engine.document, canvas.engine.revision, files.path) == before
    finally:
        canvas.dispose()
