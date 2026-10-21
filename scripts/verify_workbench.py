"""在独立 Qt 进程核验工作台深浅主题、原生窗口和三档缩放。"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def child(scale, platform):
    """每个缩放在新进程创建 QApplication，避免运行期间修改 Qt 缩放基线。"""
    from mindmap_core import Theme
    from mindmap_docking import ViewMode
    from mindmap_workbench.shell_adapter import build_window
    from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QFileDialog

    app = QApplication([])
    output = ROOT / ".artifacts/workbench-visual" / platform / f"{scale:g}x"
    output.mkdir(parents=True, exist_ok=True)
    results = []
    temporary = TemporaryDirectory(prefix="mindmap-visual-")
    data_dir = Path(temporary.name)
    for name in ("light", "dark"):
        window = build_window(storage_dir=data_dir / ("配置-" + name), theme=name, restore=False)
        try:
            window.resize(1440, 960)
            window.show()
            app.processEvents()
            assert window.canvas is None and len(window.sessions) == 0
            assert window.grab().save(str(output / (name + "-welcome.png")))
            assert window.new_document(data_dir / (name + "-主导图.mindmap"))
            primary = window.active_session
            with window.engine.transaction("建立验收导图"):
                root = window.engine.root_id
                window.engine.update_node(root, text="思维导图工作台")
                first = window.engine.add_child(root, "整理想法")
                window.engine.add_child(first, "画布与引擎保持独立")
                window.engine.add_child(first, "外壳修改只维护 桌面外壳")
                window.engine.add_child(root, "文件保存与图形导出")
            assert window.new_document(data_dir / (name + "-并行导图.mindmap"))
            secondary = window.active_session
            secondary.engine.update_node(secondary.engine.root_id, text="独立会话与历史")
            assert window.save_all()
            window.manager.activate_document(primary.id)
            window.activateWindow()
            window.manager.activate("outline", focus=False)
            app.processEvents()
            window.canvas.fit_to_view()
            app.processEvents()
            group, _ = window.host.editor.find_token(primary.id)
            controls = window.canvas._view_controls
            # 从实际视口像素检查左右顶部接缝；嵌入画布应填满矩形，不再露出内层圆角。
            viewport_image = window.canvas.view.viewport().grab().toImage()
            canvas_color = window.manager.theme.color("editor-bg")
            checks = {
                "平台": app.platformName() == platform,
                "缩放": abs(window.devicePixelRatioF() - scale) < 0.01,
                "面板尺寸": window.panels.properties.width() >= 330,
                "面板内容可见": window.panels.properties.fields["style.font_size"].isVisible(),
                "画布可用": window.canvas.view.width() > 400 and window.canvas.view.height() > 400,
                "多个独立文档": len(window.sessions) == 2 and primary.canvas is not secondary.canvas,
                "活动侧栏": window._properties_stack.currentWidget() is primary.panels.properties,
                "标签栏历史入口": group.cornerWidget().actions() == [
                    window.canvas.action("undo"), window.canvas.action("redo"),
                ],
                "无第二行工具栏": window.canvas.toolbar.isHidden(),
                "画布顶部直角衔接": all(
                    viewport_image.pixelColor(x, 0) == canvas_color
                    for x in (0, viewport_image.width() - 1)
                ),
                "画布与外壳有层次": canvas_color != window.manager.theme.color("main-window-bg"),
                "画布导航浮条": controls.isVisible()
                and window.canvas.view.viewport().geometry().contains(controls.geometry()),
            }
            # 真实菜单保留固定原生打开和保存；扩展编解码器不能增加文件格式入口。
            main_menu = window.main_menu()
            file_menu = main_menu.actions()[0].menu()
            checks["仅打开原生导图"] = (file_menu.actions()[1].text() == "打开…"
                and window.files.format_filters("open") == "思维导图 (*.mindmap)")
            checks["仅保存原生导图"] = window.files.format_filters("save") == "思维导图 (*.mindmap)"
            file_menu.popup(window.title_bar.menu.mapToGlobal(window.title_bar.menu.rect().bottomLeft()))
            app.processEvents()
            assert file_menu.grab().save(str(output / (name + "-file-menu.png")))
            file_menu.close()
            exports = next(action.menu() for action in file_menu.actions() if action.text() == "导出图形")
            checks["图形导出格式保留"] = [action.text() for action in exports.actions()] == [
                "PNG 图片…", "SVG 矢量图…", "PDF 文档…",
            ]
            exports.popup(window.title_bar.menu.mapToGlobal(window.title_bar.menu.rect().bottomRight()))
            app.processEvents()
            assert exports.grab().save(str(output / (name + "-export-menu.png")))
            exports.close()
            main_menu.deleteLater()
            # 通过真正的另存为入口打开 Qt 文件对话框，抓取明暗主题和唯一筛选。
            # 验收专用包装关闭系统原生对话框，便于同一进程读到控件并自动取消；
            # 产品仍使用原来的静态入口和系统默认选择，不改用户路径及导图内容。
            original_dialog = QFileDialog.getSaveFileName

            def capture_save_dialog(parent, title, initial, filters):
                """截图实际模态对话框后取消，验证固定筛选及取消状态不影响会话。"""
                def capture():
                    dialog = app.activeModalWidget()
                    if not isinstance(dialog, QFileDialog):
                        checks["保存对话框可见"] = False
                        app.closeAllWindows()
                        return
                    checks["保存对话框可见"] = dialog.isVisible()
                    checks["保存对话框唯一格式"] = dialog.nameFilters() == ["思维导图 (*.mindmap)"]
                    checks["保存对话框截图"] = dialog.grab().save(str(output / (name + "-save-dialog.png")))
                    dialog.reject()

                QTimer.singleShot(100, capture)
                return original_dialog(parent, title, initial, filters, options=QFileDialog.Option.DontUseNativeDialog)

            QFileDialog.getSaveFileName = capture_save_dialog
            try:
                original_path = window.files.path
                checks["取消另存为保留路径"] = not window.save(save_as=True) and window.files.path == original_path
            finally:
                QFileDialog.getSaveFileName = original_dialog
            # 层级按钮使用真实标题布局和菜单几何，逐档缩放核验，不能只检查动作存在。
            outline = window.panels.outline
            button, = window.manager.panel("outline").action_buttons
            outline.set_display_depth(1)
            window.canvas.select_nodes([first])
            app.processEvents()
            checks["大纲严格显示1层"] = not outline.isExpanded(outline.tree_model.index_for(root))
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            app.processEvents()
            checks["大纲标题菜单"] = button.isVisible() and outline.depth_menu.isVisible()
            checks["层级菜单无裁剪"] = all(
                outline.depth_menu.rect().contains(outline.depth_menu.actionGeometry(action))
                for action in outline.depth_menu.actions()
            )
            assert window.grab().save(str(output / (name + "-outline.png")))
            assert outline.depth_menu.grab().save(str(output / (name + "-outline-menu.png")))
            action = outline.depth_menu.actions()[0]
            QTest.mouseClick(outline.depth_menu, Qt.MouseButton.LeftButton,
                             pos=outline.depth_menu.actionGeometry(action).center())
            checks["大纲所有层"] = outline.isExpanded(outline.tree_model.index_for(first))
            outline.set_display_depth(2)
            assert window.grab().save(str(output / (name + ".png")))
            window.show_settings_menu()
            app.processEvents()
            checks["设置菜单入口"] = window.settings_menu.isVisible() and "settings" not in window.manager.specs
            assert window.settings_menu.grab().save(str(output / (name + "-settings-menu.png")))
            # 当前方向固定蓝底，悬停其他方向为灰底；两种状态须在不同缩放下同时可辨。
            direction = next(action.menu() for action in window.settings_menu.actions()
                             if action.menu() and action.menu().objectName() == "settings_direction")
            direction.popup(window.settings_menu.mapToGlobal(window.settings_menu.rect().topRight()))
            selected = next(action for action in direction.actions() if action.data() == "right")
            hovered = next(action for action in direction.actions() if action.data() == "left")
            direction.setActiveAction(hovered)
            app.processEvents()
            checks["设置选中与悬停"] = selected.isChecked() and not hovered.isChecked()
            checks["设置当前值摘要"] = direction.title() == "布局方向：向右"
            assert direction.grab().save(str(output / (name + "-settings-direction.png")))
            direction.close()
            assert window.grab().save(str(output / (name + "-settings.png")))
            window.settings_menu.close()
            window.manager.activate("properties")
            app.processEvents()
            original = window.panels.properties
            window.manager.set_view_mode("properties", ViewMode.UNDOCK)
            app.processEvents()
            checks["临时展开保留实例"] = window.manager.panel("properties").content.currentWidget() is original
            assert window.grab().save(str(output / (name + "-overlay.png")))
            window.manager.set_view_mode("properties", ViewMode.DOCK_PINNED)
            app.processEvents()
            field = window.panels.properties.fields["style.font_size"]
            window.activateWindow()
            field.lineEdit().setFocus()
            app.processEvents()
            field.setValue(24)
            before, revision = window.engine.document, window.engine.revision
            window.manager.set_theme("dark" if name == "light" else "light")
            checks["外观切换保留输入"] = field.hasFocus() and field.value() == 24
            checks["外观不提交文档"] = window.engine.document == before and window.engine.revision == revision
            window.panels.properties.discard_pending()
            window.manager.hide("properties")
            checks["隐藏面板交还焦点"] = window.canvas.view.hasFocus()
            window.engine.set_theme(Theme.dark())
            before = window.engine.document
            window.manager.set_theme("light")
            checks["导图主题独立"] = window.engine.document == before
            for suffix in ("png", "svg", "pdf"):
                path = output / (name + "-export." + suffix)
                window.canvas.export(path)
                checks["导出" + suffix] = path.stat().st_size > 500
            # 在真实分屏上切换焦点，验证各缩放下菜单、侧栏和历史都随所属导图。
            window.manager.activate("properties", focus=False)
            window.host.editor.split_token(secondary.id, Qt.Orientation.Horizontal)
            secondary.canvas.view.setFocus()
            app.processEvents()
            checks["分屏焦点与侧栏"] = (window.active_session is secondary
                and window._properties_stack.currentWidget() is secondary.panels.properties)
            window.manager.set_theme(name)
            primary.canvas.fit_to_view()
            secondary.canvas.fit_to_view()
            app.processEvents()
            assert window.grab().save(str(output / (name + "-sessions-split.png")))
            window.manager.restore_default_layout()
            checks["默认布局保留文件"] = (len(window.sessions) == 2
                and all(window.host.editor.find_token(item.id) for item in (primary, secondary))
                and window.active_session is secondary)
            checks["重复打开保留会话"] = window.open_path(primary.files.path) and len(window.sessions) == 2
            results.append({"主题": name, "实际倍率": window.devicePixelRatioF(),
                            "检查": checks, "通过": all(checks.values())})
        finally:
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
    temporary.cleanup()
    (output / "report.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), "utf-8")
    return 0 if all(item["通过"] for item in results) else 1


def main():
    parser = argparse.ArgumentParser(description="验证新工作台的原生窗口及三档缩放")
    parser.add_argument("--child", type=float)
    parser.add_argument("--platform", default="windows" if sys.platform == "win32" else "offscreen")
    options = parser.parse_args()
    if options.child:
        return child(options.child, options.platform)
    # Windows 的系统缩放可能已经是 150%，QT_SCALE_FACTOR 会继续相乘。
    # 先测量真实基线，再换算倍率，确保验收对象确实是 100%、150%、200%。
    probe_environment = dict(os.environ, QT_QPA_PLATFORM=options.platform, QT_SCALE_FACTOR="1")
    probe_environment.pop("QT_SCREEN_SCALE_FACTORS", None)
    probe = subprocess.run(
        [sys.executable, "-c", "from PySide6.QtWidgets import QApplication; "
         "app=QApplication([]); print(app.primaryScreen().devicePixelRatio())"],
        env=probe_environment, capture_output=True, text=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    probe.check_returncode()
    baseline = float(probe.stdout.strip())
    passed = True
    for scale in (1, 1.5, 2):
        environment = dict(os.environ, QT_QPA_PLATFORM=options.platform, QT_SCALE_FACTOR=str(scale / baseline),
                           QT_SCALE_FACTOR_ROUNDING_POLICY="PassThrough", PYTHONIOENCODING="utf-8")
        environment.pop("QT_SCREEN_SCALE_FACTORS", None)
        result = subprocess.run([sys.executable, __file__, "--child", str(scale),
                                 "--platform", options.platform], cwd=ROOT, env=environment,
                                capture_output=True, timeout=90,
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        passed &= result.returncode == 0
        print(f"{options.platform} / {scale:g}x：{'通过' if result.returncode == 0 else '失败'}", flush=True)
        if result.returncode:
            print(result.stderr.decode("utf-8", errors="replace")[-3000:])
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
