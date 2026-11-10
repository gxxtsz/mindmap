"""桌面验收导图分屏、中文输入、拖放、插件管理及重启恢复。"""

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def main():
    """只使用临时文档和配置，截图及验收结果保留在项目的 .artifacts 目录。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "windows" if sys.platform == "win32" else "offscreen")
    from mindmap_core import MindMapDocument
    from mindmap_formats import load_document, save_document
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
            notes_path = root / f"{theme}-工作笔记.mindmap"
            save_document(MindMapDocument.new("工作笔记"), notes_path)
            # 从窗口的拖放入口打开第二份导图。
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(notes_path))])
            args = (QPointF(500, 200), Qt.DropAction.CopyAction, mime,
                    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            for event in (QDragEnterEvent(*args), QDragMoveEvent(*args), QDropEvent(*args)):
                QApplication.sendEvent(window.windowHandle(), event)
                assert event.isAccepted()
            QTest.qWait(100)
            notes = window.documents.active
            assert notes.document.path == notes_path, (notes.document.path, window.status.currentMessage())
            window.host.editor.split_token(notes.id, Qt.Orientation.Horizontal)
            app.processEvents()
            window.activateWindow()
            notes.canvas.begin_edit(notes.engine.root_id)
            notes.canvas.scene.sendEvent(notes.canvas.editor, QInputMethodEvent("zhongwen", []))
            assert notes.canvas.is_composing and not window.save()
            committed = QInputMethodEvent()
            committed.setCommitString("中文输入验证")
            notes.canvas.scene.sendEvent(notes.canvas.editor, committed)
            assert window.save_all()
            expected_title = load_document(notes_path).root.text
            assert "中文输入验证" in expected_title, window.operation_log.plain_text()
            window.manager.activate_document(mindmap.id)
            mindmap.canvas.action("fit").trigger()
            app.processEvents()
            assert len(window.host.editor.groups) == 2
            assert window.grab().save(str(output / f"{theme}-mindmap-split.png"))
            plugin_ids, screenshots = [], []

            def inspect_dialog():
                """在真实模态窗口中记录插件列表，切换导图开关并保存。"""
                dialog = QApplication.activeModalWidget()
                if not isinstance(dialog, QDialog):
                    return
                try:
                    tree = dialog.findChild(QTreeWidget)
                    for index in range(tree.topLevelItemCount()):
                        item = tree.topLevelItem(index)
                        plugin_id = item.data(0, Qt.ItemDataRole.UserRole)
                        plugin_ids.append(plugin_id)
                        if plugin_id == "mindmap":
                            item.setCheckState(0, Qt.CheckState.Unchecked)
                    screenshots.append(dialog.grab().save(str(output / f"{theme}-plugins.png")))
                finally:
                    dialog.accept()

            QTimer.singleShot(150, inspect_dialog)
            window.show_plugin_manager()
            assert plugin_ids == ["mindmap"] and screenshots == [True]
            assert set(window.providers) == {"mindmap.editor"}
            ids = set(window.documents.views)
            assert window.close()
            restored = build_window(storage_dir=directory)
            assert not restored.providers
            assert set(restored.documents.views) == ids
            assert all("暂不可用" in view.title for view in restored.documents.views.values())
            restored.plugins.settings.setValue("enabled/mindmap", True)
            assert restored.close()
            enabled = build_window(storage_dir=directory)
            enabled.resize(1440, 900)
            enabled.show()
            app.processEvents()
            assert set(enabled.documents.views) == ids
            assert enabled.documents.views[notes.id].engine.document.root.text == expected_title
            assert len(enabled.host.editor.groups) == 2
            assert enabled.grab().save(str(output / f"{theme}-restored.png"))
            assert enabled.close()
            for item in (window, restored, enabled):
                item.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            results.append({"主题": theme, "平台": app.platformName(), "导图分屏": True, "导图拖放": True,
                            "中文组合输入": True, "插件管理与重启": True, "文档与布局恢复": True})
    (output / "report.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("导图桌面验收通过，覆盖明暗主题、拖放、中文输入、插件管理及重启恢复")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
