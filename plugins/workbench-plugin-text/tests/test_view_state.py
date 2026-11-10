"""文本插件在没有运行时包的组件环境中恢复独立视图。"""

from types import SimpleNamespace

from PySide6.QtWidgets import QApplication, QWidget
from workbench_plugin_text.editor import TextDocument, TextPlugin, TextView


def test_hidden_text_view_restores_scroll_after_real_viewport_is_visible(qtbot, tmp_path):
    """长文档接近末尾的位置不能被隐藏控件的默认视口提前裁掉，另一视图保持原位。"""
    parent = QWidget()
    qtbot.addWidget(parent)
    context = SimpleNamespace(ui=SimpleNamespace(parent=parent),
                              editors=SimpleNamespace(register=lambda provider: None),
                              documents=SimpleNamespace(sync=lambda view: None))
    plugin = TextPlugin(context)
    path = tmp_path / "长文本.txt"
    path.write_text("\n".join(f"第 {index} 行内容" for index in range(300)), encoding="utf-8")
    document = TextDocument(plugin, path)
    first = TextView(plugin, document)
    qtbot.addWidget(first.widget)
    first.widget.resize(400, 150)
    first.widget.show()
    QApplication.processEvents()
    bar = first.widget.verticalScrollBar()
    bar.setValue(bar.maximum() - 2)
    state = first.capture_state()
    restored = TextView(plugin, document, state=state)
    qtbot.addWidget(restored.widget)
    # 在后台加载其他资源时处理一次事件，随后才建立真实的窄视口。
    QApplication.processEvents()
    restored.widget.resize(400, 150)
    restored.widget.show()
    qtbot.waitUntil(lambda: restored.widget.verticalScrollBar().value() == state["scroll_y"])
    restored.widget.verticalScrollBar().setValue(0)
    assert first.widget.verticalScrollBar().value() == state["scroll_y"]
    assert first.widget.document() is restored.widget.document()
    restored.dispose()
    first.dispose()
