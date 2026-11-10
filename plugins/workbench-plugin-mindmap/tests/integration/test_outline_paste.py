"""真实画布粘贴后，大纲按所选层级同步当前页节点的回归验收。"""

import pytest
from mindmap_core import MindMapEngine
from mindmap_formats import encode_document
from mindmap_qt_ui.dialogs import EdrawPasteOptions
from PySide6.QtCore import QMimeData
from PySide6.QtWidgets import QApplication


def visible_texts(tree):
    """读取 Qt 大纲中实际展开的行，不借用被测代码的深度计算推导可见性。"""
    QApplication.processEvents()
    tree.doItemsLayout()
    index = tree.model().index(0, 0)
    texts = []
    while index.isValid():
        texts.append(index.data())
        index = tree.indexBelow(index)
    return texts


@pytest.mark.parametrize("page_kind", ["main", "submap", "detail", "inspiration"])
@pytest.mark.parametrize("depth", [1, 2, 3, None])
@pytest.mark.parametrize("source", ["text", "native"])
def test_paste_and_history_follow_outline_depth(
    composition_factory, monkeypatch, page_kind, depth, source,
):
    """各页的首次及连续粘贴、撤销重做都按层级显示，且选中节点不冲破层级限制。"""
    workspace = composition_factory()
    canvas, tree = workspace.canvas, workspace.outline
    engine = canvas.engine
    if page_kind in ("submap", "detail"):
        owner = engine.add_child(engine.root_id, "页面入口")
        canvas.select_nodes([owner])
        if page_kind == "submap":
            canvas.create_submap()
        else:
            canvas.create_detail_page()
    elif page_kind == "inspiration":
        idea = engine.add_inspiration("独立想法", position=(0, 0))
        canvas.show_inspiration(idea)
    root = canvas.document_view.root_id
    root_text = engine.get_node(root).text
    tree.set_display_depth(depth)
    workspace.show_tool_window("outline", focus=False)
    signals = []
    tree.display_depth_changed.connect(signals.append)

    # 两种剪贴板都走正式 paste 入口；外部文本只替换选项弹窗的交互结果，
    # 项目原生片段仍由正式编解码器解析，避免直接调用引擎而漏掉画布通知转换。
    monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options",
                        lambda *args, **kwargs: EdrawPasteOptions("append", True, True))
    branch_texts = ["粘贴主题", "子主题", "孙主题"]
    if source == "text":
        QApplication.clipboard().setText("粘贴主题\n  子主题\n    孙主题")
    else:
        fragment_engine = MindMapEngine()
        parent = fragment_engine.root_id
        for text in branch_texts:
            parent = fragment_engine.add_child(parent, text)
        mime = QMimeData()
        mime.setData("application/x-python-mindmap+json",
                     encode_document(fragment_engine.document, format_id="json"))
        QApplication.clipboard().setMimeData(mime)

    visible_branch = branch_texts if depth is None else branch_texts[:depth - 1]
    for count in (1, 2):
        # 首次粘贴把叶节点变成分支；再次粘贴向已有分支加入新的多级后代。
        # 每次都检查真实行和模型成员，区分层级没有展开与数据根本没有同步。
        canvas.select_nodes([root])
        revision = engine.revision
        pasted = canvas.paste()
        assert len(pasted) == 1
        assert canvas.selected_node_ids() == pasted
        assert engine.revision == revision + 1
        assert set(tree.tree_model.document.nodes) == set(canvas.document_view.nodes)
        assert visible_texts(tree) == [root_text] + visible_branch * count
        assert engine.undo()
        assert visible_texts(tree) == [root_text] + visible_branch * (count - 1)
        assert engine.redo()
        assert visible_texts(tree) == [root_text] + visible_branch * count
        assert tree.display_depth == depth
        assert signals == []
