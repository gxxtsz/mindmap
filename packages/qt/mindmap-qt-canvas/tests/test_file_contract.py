"""文件能力公开检查不提交输入，也不改变引擎或保存位置。"""

import pytest
from mindmap_core import FormatInfo, FormatRegistry, MindMapError
from mindmap_formats import MindmapCodec
from mindmap_qt_canvas import MindMapFileController, MindMapWidget


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
