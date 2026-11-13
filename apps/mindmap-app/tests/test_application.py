"""产品只装配导图主体，节点扩展不能成为同级编辑器。"""

import pytest
from mindmap_app.app import build_window


def test_product_directly_owns_one_application(qtbot, tmp_path):
    window = build_window(storage_dir=tmp_path / "配置", restore=False)
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    assert window.application.context is window.context
    assert not hasattr(window, "plugins") and not hasattr(window, "providers")
    assert not hasattr(window.context, "editors")
    assert window.new_document(tmp_path / "导图.mindmap")
    first = window.documents.active
    first.engine.update_node(first.engine.root_id, text="导图内容")
    assert window.new_document(tmp_path / "另一份导图.mindmap")
    assert window.application.active_session is window.documents.active
    window.context.documents.activate_view(first.id)
    assert window.documents.active is first and window.application.active_session is first
    assert first.engine.document.root.text == "导图内容"
    with pytest.raises(ValueError, match="文档尚未打开"):
        window.context.documents.activate_view("missing")
    assert window.action("app.new").isEnabled() and window.action("app.save").isEnabled()


@pytest.mark.parametrize("suffix", [".txt", ".MD", ".software-structure"])
def test_non_mindmap_files_cannot_be_top_level_documents(qtbot, tmp_path, suffix):
    window = build_window(storage_dir=tmp_path / "配置", restore=False)
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    path = tmp_path / ("来源" + suffix)
    path.write_text("文件内容", encoding="utf-8")
    assert not window.new_document(path)
    with pytest.raises(ValueError, match="只能打开"):
        window.open_path(path)
    assert not window.documents.views and path.read_text("utf-8") == "文件内容"
