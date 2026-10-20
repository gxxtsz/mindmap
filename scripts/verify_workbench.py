"""在独立 Qt 进程核验工作台深浅主题、原生窗口和三档缩放。"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def child(scale, platform):
    """每个缩放在新进程创建 QApplication，避免运行期间修改 Qt 缩放基线。"""
    from mindmap_docking import ViewMode
    from mindmap_engine import Theme
    from mindmap_workbench.shell_adapter import DOCUMENT_ID, build_window
    from PySide6.QtCore import QCoreApplication, QEvent, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    app = QApplication([])
    output = ROOT / ".artifacts/workbench-visual" / platform / f"{scale:g}x"
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for name in ("light", "dark"):
        window = build_window(storage_dir=output / ("state-" + name), theme=name, restore=False)
        try:
            with window.engine.transaction("建立验收导图"):
                root = window.engine.root_id
                window.engine.update_node(root, text="思维导图工作台")
                first = window.engine.add_child(root, "整理想法")
                window.engine.add_child(first, "画布与引擎保持独立")
                window.engine.add_child(first, "外壳修改只维护 桌面外壳")
                window.engine.add_child(root, "文件保存与图形导出")
            window.resize(1440, 960)
            window.show()
            window.activateWindow()
            window.manager.activate("outline", focus=False)
            app.processEvents()
            window.canvas.fit_to_view()
            app.processEvents()
            group, _ = window.host.editor.find_token(DOCUMENT_ID)
            controls = window.canvas._view_controls
            checks = {
                "平台": app.platformName() == platform,
                "缩放": abs(window.devicePixelRatioF() - scale) < 0.01,
                "面板尺寸": window.panels.properties.width() >= 330,
                "面板内容可见": window.panels.properties.fields["style.font_size"].isVisible(),
                "画布可用": window.canvas.view.width() > 400 and window.canvas.view.height() > 400,
                "单一文档": len(window.manager.documents) == 1,
                "标签栏历史入口": group.cornerWidget().actions() == [
                    window.canvas.action("undo"), window.canvas.action("redo"),
                ],
                "无第二行工具栏": window.canvas.toolbar.isHidden(),
                "画布导航浮条": controls.isVisible()
                and window.canvas.view.viewport().geometry().contains(controls.geometry()),
            }
            assert window.grab().save(str(output / (name + ".png")))
            window.manager.activate("settings", focus=False)
            app.processEvents()
            assert window.grab().save(str(output / (name + "-settings.png")))
            window.manager.activate("properties")
            app.processEvents()
            original = window.panels.properties
            window.manager.set_view_mode("properties", ViewMode.WINDOW)
            app.processEvents()
            checks["浮窗保留实例"] = window.manager.panel("properties").content is original
            assert window.manager.floating["properties"].grab().save(str(output / (name + "-floating.png")))
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
            QTest.keyClick(field.lineEdit(), Qt.Key.Key_F6)
            checks["返回画布焦点"] = window.canvas.view.hasFocus()
            window.engine.set_theme(Theme.dark())
            before = window.engine.document
            window.manager.set_theme("light")
            checks["导图主题独立"] = window.engine.document == before
            for suffix in ("png", "svg", "pdf"):
                path = output / (name + "-export." + suffix)
                window.canvas.export(path)
                checks["导出" + suffix] = path.stat().st_size > 500
            results.append({"主题": name, "实际倍率": window.devicePixelRatioF(),
                            "检查": checks, "通过": all(checks.values())})
        finally:
            window.engine.mark_saved()
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
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
