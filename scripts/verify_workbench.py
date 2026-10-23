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
    from mindmap_core import BranchStyle, Theme
    from mindmap_docking import ViewMode
    from mindmap_workbench.shell_adapter import build_window
    from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt, QTimer
    from PySide6.QtGui import QEnterEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QFileDialog, QLabel

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
            welcome_controls_disabled = all(
                button.isVisible() and not button.isEnabled() for button in window._status_controls.buttons.values()
            )
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
            controls = window._status_controls
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
                "空文档按钮原位禁用": welcome_controls_disabled,
                "画布没有导航浮条": all(session.canvas._view_controls is None for session in (primary, secondary)),
                "状态栏导航入口": controls.isVisible() and window.status.rect().contains(controls.geometry())
                and list(controls.buttons) == ["fit", "center", "wheel_mode"],
            }
            # 真实菜单保留固定原生打开和保存；扩展编解码器不能增加文件格式入口。
            main_menu = window.main_menu()
            view_menu = next(action.menu() for action in main_menu.actions() if action.text() == "视图")
            checks["视图菜单无重复入口"] = not any(
                action.text() in {"适应窗口", "回到中心"} for action in view_menu.actions()
            )
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
            # 最近文件菜单使用独立弹出窗口，分别抓取菜单和最小尺寸顶栏，核验三档
            # 缩放下文件名、当前项标记、入口位置和窗口控制按钮之间仍保留足够空间。
            recent_button = window.recent_files_button
            recent_menu = recent_button.menu()

            def recent_background():
                """从父级顶栏的实际合成像素读取背景，兼容透明按钮和高 DPI 截图。"""
                image = window.title_bar.grab().toImage()
                point = recent_button.mapTo(window.title_bar, QPoint(4, recent_button.height() // 2))
                ratio = image.devicePixelRatio()
                return image.pixelColor(int((point.x() + 0.5) * ratio), int((point.y() + 0.5) * ratio))

            # 原生 Windows 的系统鼠标消息不保证在自动化进程中派发给客户区。
            # 移动后显式发送进入/离开事件，验证真实控件样式，避免抓取旧悬停状态。
            window.activateWindow()
            QTest.mouseMove(window.title_bar, QPoint(window.width() // 2, window.title_bar.height() // 2))
            QTest.qWait(40)
            QTest.mouseMove(recent_button, recent_button.rect().center())
            QTest.qWait(40)
            local = recent_button.rect().center()
            QApplication.sendEvent(recent_button, QEnterEvent(
                QPointF(local), QPointF(recent_button.mapTo(window, local)),
                QPointF(recent_button.mapToGlobal(local)),
            ))
            app.processEvents()
            checks["最近文件入口仅文字"] = (recent_button.icon().isNull()
                and recent_button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextOnly)
            checks["最近文件入口悬停灰底"] = recent_background() == window.manager.theme.color("window-control-bg-hovered")
            assert window.title_bar.grab().save(str(output / (name + "-recent-hover.png")))
            QTest.mouseMove(window.title_bar, QPoint(window.width() // 2, window.title_bar.height() // 2))
            QTest.qWait(40)
            QApplication.sendEvent(recent_button, QEvent(QEvent.Type.Leave))
            app.processEvents()
            checks["最近文件入口默认透明"] = recent_background() == window.manager.theme.color("main-window-titlebar-bg")
            assert window.title_bar.grab().save(str(output / (name + "-recent-idle.png")))
            recent_menu.popup(recent_button.mapToGlobal(recent_button.rect().bottomLeft()))
            app.processEvents()
            checks["最近文件入口展开保持灰底"] = recent_background() == window.manager.theme.color("window-control-bg-hovered")
            assert window.title_bar.grab().save(str(output / (name + "-recent-open.png")))
            recent_actions = [action for action in recent_menu.actions() if action.data()]
            checks["最近文件目录与当前项"] = (
                [action.data() for action in recent_actions] == [str(secondary.files.path), str(primary.files.path)]
                and recent_actions[1].isChecked() and not recent_actions[0].isChecked()
                and all(action.toolTip() == action.data() for action in recent_actions)
            )
            assert recent_menu.grab().save(str(output / (name + "-recent-files.png")))
            recent_menu.close()
            app.processEvents()
            checks["最近文件入口收起恢复透明"] = recent_background() == window.manager.theme.color("main-window-titlebar-bg")
            window.resize(860, 540)
            app.processEvents()
            checks["最小窗口最近文件入口"] = (
                window.title_bar.menu.geometry().right() < recent_button.x()
                and recent_button.geometry().right() < window.title_bar.minimize.x()
                and window.title_bar.rect().contains(recent_button.geometry())
            )
            assert window.grab().save(str(output / (name + "-recent-minimum.png")))
            window.resize(1440, 960)
            app.processEvents()
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
            # 设置菜单只保留应用选项；滚轮直接在状态栏切换，导图布局和外观从侧栏进入。
            settings_menu = window.appearance_menu()
            settings_menu.popup(window.title_bar.menu.mapToGlobal(QPoint(0, window.title_bar.menu.height())))
            app.processEvents()
            checks["设置菜单无重复入口"] = (
                settings_menu.actions()[0].text() == "浅色"
                and not any(action.text().startswith(("滚轮", "导图设置", "导图布局", "连接线样式", "导图主题"))
                            for action in settings_menu.actions())
            )
            assert settings_menu.grab().save(str(output / (name + "-settings-menu.png")))
            settings_menu.close()
            settings_menu.deleteLater()
            # 对每个主题及实际显示倍率检查两个图标状态、紧凑布局及消息覆盖边界。
            wheel_button = controls.buttons["wheel_mode"]
            document, revision = window.engine.document, window.engine.revision
            initial_icon = wheel_button.icon().cacheKey()
            toggles = []
            for mode in ("zoom", "scroll"):
                QTest.mouseClick(wheel_button, Qt.MouseButton.LeftButton)
                app.processEvents()
                toggles.append(window.canvas.wheel_mode == secondary.canvas.wheel_mode == mode
                               and wheel_button.menu() is None and not wheel_button.isCheckable()
                               and not wheel_button.isChecked() and QApplication.activePopupWidget() is None)
                assert window.status.grab().save(str(output / f"{name}-status-{mode}.png"))
                if mode == "zoom":
                    checks["滚轮通过图标表示模式"] = wheel_button.icon().cacheKey() != initial_icon
            checks["滚轮单击双向切换"] = all(toggles)
            checks["切换滚轮不改文档"] = window.engine.document == document and window.engine.revision == revision
            version_label = next(label for label in window.status.findChildren(QLabel)
                                 if "思维导图工作台 ·" in label.text())
            for compact in (True, False):
                window.manager.set_option("compact", compact)
                window.show_status_message("验证临时状态消息不会覆盖右侧按钮。" * 30)
                app.processEvents()
                checks[f"状态栏{'紧凑' if compact else '普通'}模式无裁切"] = (
                    window.status.height() == (24 if compact else 28)
                    and window.status.rect().contains(controls.geometry())
                    and controls.x() > version_label.geometry().right()
                    and all(button.isVisible() and controls.rect().contains(button.geometry())
                            for button in controls.buttons.values())
                )
                assert window.status.grab().save(str(output / f"{name}-status-{'compact' if compact else 'normal'}.png"))
            window.status.clearMessage()
            window.activateWindow()
            window.canvas.view.setFocus()
            # 离屏平台的窗口激活由事件循环完成，先处理焦点事件再发真实快捷键。
            app.processEvents()
            QTest.keyClick(window.canvas.view, Qt.Key.Key_3, Qt.KeyboardModifier.AltModifier)
            app.processEvents()
            settings = window.panels.settings
            checks["布局侧栏入口"] = settings.isVisible() and not window.panels.properties.isVisible()
            checks["侧栏包含布局和外观"] = list(settings.sections) == ["导图布局", "导图外观"]
            checks["整图外观控件可见"] = settings.branch.isVisible() and settings.theme.isVisible()
            # 从真实控件几何及可见标签验收紧凑样式，所有尺寸均为逻辑像素。
            picker = settings.direction
            checks["布局入口紧凑"] = (picker.width(), picker.height()) == (120, 34)
            checks["布局入口文字完整"] = (
                picker.fontMetrics().horizontalAdvance(picker.currentText()) <= picker.width() - 66
            )
            checks["布局无底部提示"] = not any(
                label.isVisible() and ("间距按100%" in label.text() or "应用于整张导图" in label.text())
                for label in settings.findChildren(QLabel)
            )
            window.canvas.fit_to_view()
            app.processEvents()
            assert window.grab().save(str(output / (name + "-settings.png")))
            assert settings.grab().save(str(output / (name + "-layout-sidebar.png")))
            # 当前方向固定蓝底，悬停其他方向为灰底；两种状态须在不同缩放下同时可辨。
            settings.direction.showPopup()
            direction = settings.direction.popup_menu
            selected = next(action for action in direction.actions() if action.data() == "right")
            hovered = next(action for action in direction.actions() if action.data() == "left")
            direction.setActiveAction(hovered)
            app.processEvents()
            checks["布局菜单紧凑"] = direction.width() == 144 and all(
                direction.actionGeometry(action).height() == 28 for action in direction.actions()
            )
            anchor_right = picker.mapToGlobal(QPoint(picker.width(), 0)).x()
            available = picker.screen().availableGeometry()
            can_align = available.left() <= anchor_right - direction.width() and anchor_right <= available.right() + 1
            # 高缩放下测试窗口可能超出屏幕，此时菜单应遵守 Qt 屏幕避让，
            # 不要求它继续跟随已经位于屏幕外的入口；正常情况仍核对侧栏内对齐。
            checks["布局菜单向侧栏内展开"] = (
                abs(direction.x() + direction.width() - anchor_right) <= 1
                and direction.x() >= settings.mapToGlobal(QPoint(0, 0)).x()
            ) if can_align else (
                direction.x() >= available.left() and direction.x() + direction.width() <= available.right() + 1
            )
            checks["布局菜单无系统投影"] = bool(direction.windowFlags() & Qt.WindowType.NoDropShadowWindowHint)
            checks["设置选中与悬停"] = selected.isChecked() and not hovered.isChecked()
            checks["设置当前值摘要"] = settings.direction.currentText() == "向右"
            assert direction.grab().save(str(output / (name + "-settings-direction.png")))
            direction.close()
            # 新迁入侧栏的两种选择器也检查真实弹层、选择与撤销；截图仍采用
            # 当前外壳色板，导图主题变化不能修改应用主题或污染其他标签。
            before = window.engine.document
            settings.branch.showPopup()
            app.processEvents()
            branch_menu = settings.branch.popup_menu
            checks["整图连接线四列网格"] = len(branch_menu.buttons) == 11 and all(
                button.isVisible() and button.parentWidget().rect().contains(button.geometry())
                for button in branch_menu.buttons.values()
            )
            assert branch_menu.grab().save(str(output / (name + "-settings-branch.png")))
            QTest.mouseClick(branch_menu.buttons[BranchStyle.ARROW], Qt.MouseButton.LeftButton)
            checks["整图连接线可撤销"] = (
                all(node.style["branch"]["kind"] == "arrow" for node in window.engine.document.nodes.values())
                and window.engine.undo() and window.engine.document == before
            )
            settings.theme.showPopup()
            app.processEvents()
            theme_view = settings.theme.view()
            checks["导图主题选项完整"] = [settings.theme.itemText(index) for index in range(settings.theme.count())] == [
                "浅色", "深色", "自定义",
            ] and not settings.theme.model().item(settings.theme.findData("custom")).isEnabled()
            assert theme_view.window().grab().save(str(output / (name + "-settings-theme.png")))
            target = settings.theme.model().index(settings.theme.findData("dark"), 0)
            # Windows 原生组合框会短暂屏蔽展开后的快速鼠标松开，防止同一次
            # 点击误选选项。按系统双击间隔等待后再点击，模拟正常的用户选择。
            QTest.qWait(QApplication.doubleClickInterval())
            QTest.mouseClick(theme_view.viewport(), Qt.MouseButton.LeftButton,
                             pos=theme_view.visualRect(target).center())
            checks["导图主题实际选择"] = window.engine.document.theme == Theme.dark()
            checks["导图主题与应用及其他标签独立"] = (
                window.manager.theme.name == name and secondary.engine.document.theme == Theme()
            )
            checks["导图主题可撤销"] = window.engine.undo() and window.engine.document == before
            settings.horizontal.setValue(80)
            settings.vertical.setValue(30)
            settings.flush()
            checks["布局自定义间距"] = window.engine.document.layout.options == {
                "level_gap": 80, "sibling_gap": 30,
            }
            checks["布局自定义预设"] = not any(button.isChecked() for button in settings.preset_buttons)
            window.host.region_sizes["dock"]["right"] = 260
            window.manager.refresh()
            app.processEvents()
            window.canvas.fit_to_view()
            app.processEvents()
            checks["窄侧栏无水平滚动"] = settings.scroll.horizontalScrollBar().maximum() == 0
            checks["窄侧栏字段完整"] = all(
                field.parentWidget().width() >= field.width()
                for field in (settings.horizontal, settings.vertical, settings.branch, settings.theme)
            )
            assert window.grab().save(str(output / (name + "-layout-narrow.png")))
            window.host.region_sizes["dock"]["right"] = 360
            window.manager.refresh()
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
