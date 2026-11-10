"""桌面验收两个插件的混合分屏、中文输入、拖放、插件管理及重启恢复。"""

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def main():
    """只使用临时文档和配置，截图及验收结果保留在项目的 .artifacts 目录。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "windows" if sys.platform == "win32" else "offscreen")
    from personal_workbench.app import build_window
    from PySide6.QtCore import (
        QCoreApplication,
        QEvent,
        QLibraryInfo,
        QMimeData,
        QPointF,
        Qt,
        QTimer,
        QTranslator,
        QUrl,
    )
    from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent, QInputMethodEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QDialog, QTreeWidget

    app = QApplication([])
    translator = QTranslator(app)
    translator.load("qtbase_zh_CN", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
    app.installTranslator(translator)
    output = Path(__file__).resolve().parents[1] / ".artifacts/plugins-visual"
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with TemporaryDirectory(prefix="workbench-plugins-") as temporary:
        root = Path(temporary).resolve()
        for theme in ("light", "dark"):
            directory = root / theme
            window = build_window(storage_dir=directory, theme=theme, restore=False)
            window.resize(1440, 900)
            window.show()
            window.activateWindow()
            app.processEvents()
            assert window.new_document(root / f"{theme}-工作计划.mindmap")
            mindmap = window.documents.active
            mindmap.engine.update_node(mindmap.engine.root_id, text="个人工作台")
            for label in ("整理思路", "编写文档", "扩展自己的工具"):
                mindmap.engine.add_child(mindmap.engine.root_id, label)
            text_path = root / f"{theme}-工作笔记.md"
            text_path.write_text("# 工作笔记\n\n今天的计划\n\n- 整理项目思路\n- 记录实现过程\n- 验证插件之间的协作\n", encoding="utf-8")
            # 使用真实窗口拖放事件打开文本，验证资源选择不依赖当前导图插件。
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(text_path))])
            args = (QPointF(500, 200), Qt.DropAction.CopyAction, mime,
                    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            for event in (QDragEnterEvent(*args), QDragMoveEvent(*args), QDropEvent(*args)):
                QApplication.sendEvent(window.windowHandle(), event)
                assert event.isAccepted()
            QTest.qWait(100)
            text = window.documents.active
            assert text.document.path == text_path, (text.document.path, window.status.currentMessage())
            window.host.editor.split_token(text.id, Qt.Orientation.Horizontal)
            app.processEvents()
            window.activateWindow()
            text.widget.setFocus()
            app.processEvents()
            candidate = QInputMethodEvent("zhongwen", [])
            QApplication.sendEvent(text.widget, candidate)
            assert not window.save()
            committed = QInputMethodEvent()
            committed.setCommitString("中文输入验证\n\n")
            QApplication.sendEvent(text.widget, committed)
            assert window.save_all()
            assert "中文输入验证" in text_path.read_text("utf-8"), (
                text.widget.toPlainText(), text.document.content.toPlainText(),
                [(type(item).__name__, str(item.document.path)) for item in window.documents.ordered()],
                window.operation_log.plain_text())
            window.manager.activate_document(mindmap.id)
            app.processEvents()
            mindmap.canvas.action("fit").trigger()
            app.processEvents()
            assert window.grab().save(str(output / f"{theme}-mixed-mindmap.png"))
            window.manager.activate_document(text.id)
            app.processEvents()
            assert not window.manager.is_tool_available("properties")
            assert window.grab().save(str(output / f"{theme}-mixed-text.png"))
            checked = []

            def inspect_dialog():
                """在真实模态窗口中保存文本插件开关，验证本次运行不立即卸载编辑器。"""
                dialog = QApplication.activeModalWidget()
                if not isinstance(dialog, QDialog):
                    return
                tree = dialog.findChild(QTreeWidget)
                assert tree.topLevelItemCount() == 2
                for index in range(tree.topLevelItemCount()):
                    item = tree.topLevelItem(index)
                    if item.data(0, Qt.ItemDataRole.UserRole) == "text":
                        item.setCheckState(0, Qt.CheckState.Unchecked)
                assert dialog.grab().save(str(output / f"{theme}-plugins.png"))
                checked.append(True)
                dialog.accept()

            QTimer.singleShot(150, inspect_dialog)
            window.show_plugin_manager()
            assert checked and "text.editor" in window.providers
            ids = set(window.documents.views)
            assert window.close()
            restored = build_window(storage_dir=directory)
            assert "text.editor" not in restored.providers
            assert set(restored.documents.views) == ids
            assert "暂不可用" in restored.documents.views[text.id].title
            restored.plugins.settings.setValue("enabled/text", True)
            assert restored.close()
            enabled = build_window(storage_dir=directory)
            enabled.resize(1440, 900)
            enabled.show()
            app.processEvents()
            assert set(enabled.documents.views) == ids
            assert enabled.documents.views[text.id].widget.toPlainText() == text_path.read_text("utf-8")
            assert len(enabled.host.editor.groups) == 2
            assert enabled.grab().save(str(output / f"{theme}-restored.png"))
            assert enabled.close()
            for item in (window, restored, enabled):
                item.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            results.append({"主题": theme, "平台": app.platformName(), "混合分屏": True, "文本拖放": True,
                            "中文组合输入": True, "插件管理与重启": True, "文档与布局恢复": True})
    (output / "report.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("混合编辑器桌面验收通过：明暗主题、拖放、中文输入、插件管理及重启恢复")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
