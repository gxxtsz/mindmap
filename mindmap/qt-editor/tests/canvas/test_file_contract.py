"""导图目录的共享保存状态及冲突检查。"""

from mindmap_core import MindMapDocument, MindMapEngine
from mindmap_core.formats import load_directory, save_directory
from mindmap_qt_editor.canvas import MindMapFileController, MindMapFileState, MindMapWidget


def test_file_controllers_can_share_path_and_cancel_before_submitting(qtbot, tmp_path, monkeypatch, choose_save_directory):
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
    choose_save_directory(target)
    first.prepare_save = lambda: False
    try:
        assert not first.save(save_as=True) and not calls and not target.exists()
        first.prepare_save = lambda: True
        assert first.save(save_as=True)
        assert first.path == second.path == target and independent.path is None
        second.error_occurred.connect(errors.append)
        occupied = tmp_path / "已占用"
        occupied.mkdir()
        choose_save_directory(occupied)
        assert not second.save(save_as=True)
        assert first.path == second.path == target and errors
    finally:
        for canvas in canvases:
            canvas.dispose()


def test_directory_conflict_does_not_submit_inputs_or_change_path(qtbot, tmp_path):
    canvas = MindMapWidget()
    qtbot.addWidget(canvas)

    class Inputs:
        def flush_pending_edits(self):
            raise AssertionError("已提交快照不应提交侧栏输入")

    files = MindMapFileController(canvas, Inputs())
    target = tmp_path / "导图"
    version = save_directory(canvas.engine.document, target)
    files.accept_directory_version(target, version)
    before = canvas.engine.document, canvas.engine.revision
    try:
        external = MindMapDocument.new("外部修改")
        save_directory(external, target)
        assert not files.save_committed() and files.state.conflict
        assert files.path == target and load_directory(target) == external
        assert (canvas.engine.document, canvas.engine.revision) == before
    finally:
        canvas.dispose()
